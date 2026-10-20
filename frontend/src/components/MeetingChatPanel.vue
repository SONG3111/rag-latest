<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { SendOutlined, ToolOutlined } from '@ant-design/icons-vue'
import { streamMeetingChat } from '@/composables/useChatStream'
import type { AssistantActivity, MeetingChatTurn } from '@/types'
import MarkdownContent from './MarkdownContent.vue'

const props = defineProps<{
  meetingId: string
  disabled?: boolean
  unavailableMessage?: string
}>()

let counter = 0
const nextId = (prefix: string) => `${prefix}-${Date.now()}-${(counter += 1)}`

const turns = ref<MeetingChatTurn[]>([])
const draft = ref('')
const streaming = ref(false)
const scroller = ref<HTMLElement | null>(null)

watch(
  () => props.meetingId,
  () => {
    turns.value = []
    draft.value = ''
  },
)

async function scrollToBottom() {
  await nextTick()
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight
}

async function send() {
  const content = draft.value.trim()
  if (!content || streaming.value || props.disabled) return
  const history = turns.value
    .filter((turn) => turn.content.trim())
    .slice(-12)
    .map((turn) => ({ role: turn.role, content: turn.content }))
  draft.value = ''
  const userTurn: MeetingChatTurn = {
    id: nextId('user'),
    role: 'user',
    content,
    activities: [],
    streaming: false,
  }
  const assistantId = nextId('assistant')
  const assistantTurn: MeetingChatTurn = {
    id: assistantId,
    role: 'assistant',
    content: '',
    activities: [],
    streaming: true,
  }
  turns.value.push(userTurn, assistantTurn)
  streaming.value = true
  await scrollToBottom()

  try {
    await streamMeetingChat(props.meetingId, content, history, {
      onToken: (payload) => {
        const turn = turns.value.find((item) => item.id === assistantId)
        if (turn) turn.content += payload.text
      },
      onToolCall: (payload) => {
        const turn = turns.value.find((item) => item.id === assistantId)
        if (!turn) return
        turn.activities.push({
          id: nextId('activity'),
          tool: payload.tool,
          label: payload.label,
          status: 'running',
        })
      },
      onToolResult: () => {
        const turn = turns.value.find((item) => item.id === assistantId)
        if (!turn) return
        const running = [...turn.activities]
          .reverse()
          .find((item: AssistantActivity) => item.status === 'running')
        if (running) running.status = 'done'
      },
      onDone: (payload) => {
        const turn = turns.value.find((item) => item.id === assistantId)
        if (turn && payload.content) turn.content = payload.content
      },
      onError: (payload) => {
        const turn = turns.value.find((item) => item.id === assistantId)
        if (turn) turn.error = payload.message
      },
    })
  } catch (error) {
    const turn = turns.value.find((item) => item.id === assistantId)
    if (turn) turn.error = error instanceof Error ? error.message : String(error)
  } finally {
    const turn = turns.value.find((item) => item.id === assistantId)
    if (turn) turn.streaming = false
    streaming.value = false
    await scrollToBottom()
  }
}
</script>

<template>
  <section class="meeting-chat">
    <div class="chat-head">
      <span>追问会议内容</span>
      <span class="privacy">回答只引用本场转写并带时间戳</span>
    </div>
    <div ref="scroller" class="chat-body">
      <div v-if="turns.length === 0" class="chat-empty">
        {{
          unavailableMessage ||
          '例如：谁负责上线？有哪些风险还没有确认？预算数字是多少？'
        }}
      </div>
      <div
        v-for="turn in turns"
        :key="turn.id"
        class="chat-turn"
        :class="turn.role"
      >
        <div class="chat-bubble">
          <div v-if="turn.activities.length" class="chat-tools">
            <div v-for="activity in turn.activities" :key="activity.id" class="chat-tool">
              <ToolOutlined />
              <span>{{ activity.label }}</span>
              <span class="tool-state">{{ activity.status === 'running' ? '检索中' : '完成' }}</span>
            </div>
          </div>
          <MarkdownContent
            v-if="turn.role === 'assistant' && turn.content"
            :text="turn.content"
          />
          <div v-else-if="turn.content" class="plain" v-text="turn.content" />
          <div v-if="turn.streaming && !turn.content" class="thinking">正在查找转写…</div>
          <div v-if="turn.error" class="chat-error">{{ turn.error }}</div>
        </div>
      </div>
    </div>
    <div class="chat-composer">
      <a-textarea
        v-model:value="draft"
        :auto-size="{ minRows: 1, maxRows: 4 }"
        :disabled="disabled || streaming"
        placeholder="基于本场会议提问…"
        @press-enter.exact.prevent="send"
      />
      <a-button
        type="primary"
        :loading="streaming"
        :disabled="disabled || !draft.trim()"
        @click="send"
      >
        <template #icon><SendOutlined /></template>
        追问
      </a-button>
    </div>
  </section>
</template>

<style scoped>
.meeting-chat {
  border-left: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  height: 100%;
}

.chat-head {
  padding: 12px 18px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  font-size: 13px;
  font-weight: 600;
  flex: 0 0 auto;
}

.privacy {
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 400;
}

.chat-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 0 18px 8px;
}

.chat-empty {
  color: var(--text-muted);
  font-size: 12px;
  padding: 8px 0 14px;
}

.chat-turn {
  display: flex;
  margin: 8px 0;
}

.chat-turn.user {
  justify-content: flex-end;
}

.chat-bubble {
  max-width: 88%;
  padding: 9px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface-muted);
  font-size: 13px;
  line-height: 1.65;
}

.chat-turn.user .chat-bubble {
  background: #edf4ff;
}

.plain {
  white-space: pre-wrap;
}

.chat-tools {
  margin-bottom: 6px;
}

.chat-tool {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--text-muted);
  font-size: 11px;
}

.tool-state {
  color: var(--success);
}

.thinking,
.chat-error {
  color: var(--text-muted);
  font-size: 12px;
}

.chat-error {
  color: var(--danger);
}

.chat-composer {
  padding: 10px 18px 14px;
  display: flex;
  align-items: flex-end;
  gap: 8px;
  border-top: 1px solid var(--border);
  flex: 0 0 auto;
}

.chat-composer :deep(textarea) {
  resize: none;
}
</style>
