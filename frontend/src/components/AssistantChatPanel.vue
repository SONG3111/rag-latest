<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { SendOutlined, StopOutlined, ToolOutlined } from '@ant-design/icons-vue'
import { useAssistantStore } from '@/stores/assistant'
import MarkdownContent from './MarkdownContent.vue'
import AssistantActionCard from './AssistantActionCard.vue'

const store = useAssistantStore()
const draft = ref('')
const scroller = ref<HTMLElement | null>(null)
const textareaRef = ref<{ resizableTextArea?: { textArea?: HTMLTextAreaElement } } | null>(null)
const composing = ref(false)
const composingText = ref('')
let clearDuringComposition = false
let sentWhileComposing: string | null = null

const composerText = computed(() => (composing.value ? composingText.value : draft.value))
const examples = [
  '查一下张三最近发来的邮件',
  '帮我回复这封邮件，说周三下午可以',
  '看看明天下午有没有空，和张三开会',
]

function textareaEl(): HTMLTextAreaElement | null {
  return textareaRef.value?.resizableTextArea?.textArea ?? null
}

function onDraftInput(value: string) {
  if (composing.value) {
    composingText.value = value
    return
  }
  draft.value = value
}

function onCompositionStart() {
  composing.value = true
  composingText.value = ''
}

function onCompositionEnd() {
  if (!composing.value) return
  composing.value = false
  const el = textareaEl()
  if (clearDuringComposition && el && el.value === sentWhileComposing) el.value = ''
  clearDuringComposition = false
  sentWhileComposing = null
  draft.value = el ? el.value : ''
  composingText.value = ''
}

function onPressEnter(event: KeyboardEvent) {
  if (composing.value || event.isComposing || event.keyCode === 229) return
  if (!event.shiftKey) {
    event.preventDefault()
    send()
  }
}

async function scrollToBottom() {
  await nextTick()
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight
}

watch(() => store.turns.length, scrollToBottom)
watch(
  () => store.turns.map((turn) => turn.activities.length).join(','),
  scrollToBottom,
)

async function send(text?: string) {
  const content = (text ?? composerText.value).trim()
  if (!content || store.streaming) return
  if (composing.value) {
    clearDuringComposition = true
    sentWhileComposing = content
  }
  draft.value = ''
  composingText.value = ''
  await store.send(content)
  await scrollToBottom()
}
</script>

<template>
  <main class="panel assistant-chat">
    <div class="panel-header">
      <span>邮件与日程助手</span>
      <span v-if="store.streaming" class="streaming">Agent 正在处理…</span>
    </div>

    <div ref="scroller" class="panel-body conversation">
      <div v-if="store.turns.length === 0" class="empty welcome">
        <div class="welcome-title">从飞书或这里发指令</div>
        <div class="welcome-sub">
          可以搜索和阅读飞书邮箱、准备回复草稿、检查空闲时间并安排飞书会议。<br />
          保存草稿、发送邮件和创建会议都会先生成待确认操作。
        </div>
        <div class="examples">
          <a-tag v-for="example in examples" :key="example" class="example" @click="send(example)">
            {{ example }}
          </a-tag>
        </div>
      </div>

      <div v-for="turn in store.turns" :key="turn.id" class="turn" :class="turn.role">
        <div class="bubble">
          <div v-if="turn.activities.length" class="activities">
            <div
              v-for="activity in turn.activities"
              :key="activity.id"
              class="activity"
              :class="activity.status"
            >
              <ToolOutlined />
              <span>{{ activity.label }}</span>
              <a-spin v-if="activity.status === 'running'" size="small" />
              <span v-else class="done">完成</span>
            </div>
          </div>
          <MarkdownContent
            v-if="turn.content && turn.role === 'assistant'"
            class="content"
            :text="turn.content"
          />
          <div v-else-if="turn.content" class="content plain" v-text="turn.content" />
          <div v-if="turn.streaming && !turn.content" class="thinking muted">
            <a-spin size="small" /> 正在处理…
          </div>
          <div v-if="turn.error" class="turn-error">{{ turn.error }}</div>
          <AssistantActionCard
            v-for="action in turn.proposals"
            :key="action.id"
            :action="action"
          />
        </div>
      </div>
    </div>

    <div class="composer">
      <a-textarea
        ref="textareaRef"
        :value="draft"
        :auto-size="{ minRows: 1, maxRows: 5 }"
        placeholder="例如：查一下 Bob 上周发来的邮件，帮我准备回复"
        :disabled="store.streaming"
        @update:value="onDraftInput"
        @compositionstart="onCompositionStart"
        @compositionend="onCompositionEnd"
        @press-enter="onPressEnter"
      />
      <a-button v-if="store.streaming" danger @click="store.stop()">
        <template #icon><StopOutlined /></template>
        停止
      </a-button>
      <a-button v-if="!store.streaming" type="primary" :disabled="!composerText.trim()" @click="send()">
        <template #icon><SendOutlined /></template>
        发送
      </a-button>
    </div>
  </main>
</template>

<style scoped>
.assistant-chat {
  min-width: 0;
  border-right: 1px solid var(--border);
}

.conversation {
  padding: 20px 24px;
}

.streaming {
  color: var(--accent);
  font-size: 12px;
  font-weight: 400;
}

.welcome {
  padding-top: 12vh;
}

.welcome-title {
  color: var(--text);
  font-size: 18px;
  font-weight: 600;
}

.welcome-sub {
  margin-top: 10px;
  line-height: 1.9;
  color: var(--text-muted);
}

.examples {
  margin-top: 22px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
}

.example {
  cursor: pointer;
  padding: 5px 12px;
  border-radius: 999px;
  font-size: 12px;
  background: var(--surface-muted);
  border: 1px solid var(--border);
}

.turn {
  display: flex;
  margin-bottom: 18px;
}

.turn.user {
  justify-content: flex-end;
}

.bubble {
  max-width: 82%;
  border-radius: 12px;
  padding: 12px 15px;
  font-size: 14px;
  line-height: 1.75;
  background: var(--surface-muted);
  border: 1px solid var(--border);
}

.activities {
  margin-bottom: 8px;
}

.activity {
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--text-muted);
  font-size: 12px;
  padding: 2px 0;
}

.activity.done,
.done {
  color: var(--success);
}

.thinking {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}

.turn-error {
  color: var(--danger);
  font-size: 13px;
}

.composer {
  padding: 12px 16px;
  border-top: 1px solid var(--border);
  display: flex;
  gap: 10px;
  align-items: flex-end;
}

.composer :deep(textarea) {
  resize: none;
}

</style>
