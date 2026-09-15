<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import {
  DislikeOutlined,
  LikeOutlined,
  SendOutlined,
  StopOutlined,
  ToolOutlined,
} from '@ant-design/icons-vue'
import { useWorkspaceStore } from '@/stores/workspace'
import MarkdownContent from './MarkdownContent.vue'
import OperationCard from './OperationCard.vue'
import ThinkingBlock from './ThinkingBlock.vue'

const store = useWorkspaceStore()
const draft = ref('')
const scroller = ref<HTMLElement | null>(null)
const textareaRef = ref<{ resizableTextArea?: { textArea?: HTMLTextAreaElement } } | null>(null)

// IME 组合输入守卫。ant-design-vue 的 TextArea 在组合结束时会读取 textarea 的实时
// DOM 值并强制 emit update:value（TextArea.js 的 onInternalCompositionEnd）——发送
// 清空 draft 之后，仍在收尾的输入法组合就会把刚发送的原文重新写回输入框。Vue 原生
// v-model 通过忽略拼字阶段的更新来规避这个问题（官方文档"表单输入绑定"），这里在
// 组件层补齐同样的语义：拼字期间 draft 不跟随，组合结束时一次性同步。
const composing = ref(false)
const composingText = ref('')
let clearDuringComposition = false
let sentWhileComposing: string | null = null

const composerText = computed(() => (composing.value ? composingText.value : draft.value))

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
  if (clearDuringComposition && el && el.value === sentWhileComposing) {
    // 发生在拼字中的发送：输入法把刚发送的原文写回了缓冲，直接清掉
    el.value = ''
  }
  clearDuringComposition = false
  sentWhileComposing = null
  draft.value = el ? el.value : ''
  composingText.value = ''
}

function onPressEnter(event: KeyboardEvent) {
  // 拼字阶段的 Enter 属于输入法（选词/上屏），不触发发送
  if (composing.value || event.isComposing || event.keyCode === 229) return
  if (!event.shiftKey) {
    event.preventDefault()
    send()
  }
}

const examples = [
  '制度里规定的单笔报销上限是多少？',
  '工作区里有哪些文件？',
  '把销售表里 A型 的销售额改成 1500',
  '按制度规定的上限，检查销售表里有没有超标的数据',
]

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
  if (!store.activeWorkspaceId) {
    message.warning('请先选择或新建一个工作区')
    return
  }
  if (composing.value) {
    // 拼字中途发送：组合收尾时不再把缓冲里的已发送原文同步回来
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
  <main class="panel chat-panel">
    <div class="panel-header">
      <span>{{ store.activeWorkspace?.name ?? '对话' }}</span>
      <span v-if="store.streaming" class="streaming-hint">Agent 正在处理…</span>
    </div>

    <div ref="scroller" class="panel-body conversation">
      <div v-if="store.turns.length === 0" class="empty welcome">
        <div class="welcome-title">和工作区里的文档对话</div>
        <div class="welcome-sub">
          上传 Excel / Word 之后，可以直接提问，也可以让 Agent 帮你改表格——<br />
          所有修改都会先给你看变更预览，确认后才写入文件。
        </div>
        <div class="examples">
          <a-tag
            v-for="example in examples"
            :key="example"
            class="example"
            @click="send(example)"
          >
            {{ example }}
          </a-tag>
        </div>
      </div>

      <div v-for="turn in store.turns" :key="turn.id" class="turn" :class="turn.role">
        <div class="bubble">
          <div v-if="turn.activities.length > 0" class="activities">
            <div
              v-for="activity in turn.activities"
              :key="activity.id"
              class="activity"
              :class="activity.status"
            >
              <ToolOutlined class="activity-icon" />
              <span class="activity-label">{{ activity.label }}</span>
              <a-spin v-if="activity.status === 'running'" size="small" />
              <span v-else-if="activity.status === 'done'" class="activity-done">完成</span>
            </div>
          </div>

          <ThinkingBlock
            v-if="turn.thinking && turn.role === 'assistant'"
            :text="turn.thinking"
            :active="turn.streaming && !turn.content"
          />

          <div v-if="turn.notice" class="turn-notice">{{ turn.notice }}</div>

          <MarkdownContent
            v-if="turn.content && turn.role === 'assistant'"
            class="content"
            :text="turn.content"
          />
          <div v-else-if="turn.content" class="content plain" v-text="turn.content" />

          <div v-if="turn.streaming && !turn.content && !turn.thinking" class="thinking muted">
            <a-spin size="small" /> 正在思考…
          </div>

          <div v-if="turn.error" class="turn-error">{{ turn.error }}</div>

          <div
            v-if="turn.role === 'assistant' && turn.messageId && !turn.streaming"
            class="feedback-row"
          >
            <button
              type="button"
              class="feedback-btn"
              :class="{ active: turn.feedback === 'up' }"
              title="有帮助"
              @click="store.rate(turn.id, 'up')"
            >
              <LikeOutlined />
            </button>
            <button
              type="button"
              class="feedback-btn"
              :class="{ active: turn.feedback === 'down' }"
              title="没帮助"
              @click="store.rate(turn.id, 'down')"
            >
              <DislikeOutlined />
            </button>
          </div>

          <OperationCard
            v-for="operation in turn.proposals"
            :key="operation.id"
            :operation="operation"
          />
        </div>
      </div>
    </div>

    <div v-if="store.followups.length && !store.streaming" class="followups">
      <a-tag
        v-for="question in store.followups"
        :key="question"
        class="followup"
        @click="send(question)"
      >
        {{ question }}
      </a-tag>
    </div>

    <div class="composer">
      <a-textarea
        ref="textareaRef"
        :value="draft"
        :auto-size="{ minRows: 1, maxRows: 5 }"
        placeholder="描述你想查询或修改的内容，Enter 发送，Shift+Enter 换行"
        :disabled="store.streaming"
        @update:value="onDraftInput"
        @compositionstart="onCompositionStart"
        @compositionend="onCompositionEnd"
        @press-enter="onPressEnter"
      />
      <a-button
        v-if="store.streaming"
        danger
        @click="store.stop()"
      >
        <template #icon><StopOutlined /></template>
        停止生成
      </a-button>
      <a-button
        v-else
        type="primary"
        :disabled="!composerText.trim() || !store.activeWorkspaceId"
        @click="send()"
      >
        <template #icon><SendOutlined /></template>
        发送
      </a-button>
    </div>
  </main>
</template>

<style scoped>
.chat-panel {
  min-width: 0;
}

.conversation {
  padding: 20px 24px;
}

.streaming-hint {
  font-size: 12px;
  font-weight: 400;
  color: var(--accent);
}

.welcome {
  padding-top: 12vh;
}

.welcome-title {
  font-size: 18px;
  font-weight: 600;
  color: var(--text);
}

.welcome-sub {
  margin-top: 10px;
  color: var(--text-muted);
  line-height: 1.9;
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

.example:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.turn {
  display: flex;
  margin-bottom: 18px;
}

.turn.user {
  justify-content: flex-end;
}

.bubble {
  max-width: 78%;
  border-radius: 12px;
  padding: 12px 15px;
  font-size: 14px;
  line-height: 1.75;
  background: var(--surface-muted);
  border: 1px solid var(--border);
}

.turn.user .bubble {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}

.content {
  word-break: break-word;
}

.content.plain {
  white-space: pre-wrap;
}

.activities {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 10px;
}

.turn.user .activities {
  display: none;
}

.activity {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-muted);
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 6px 10px;
}

.activity-icon {
  color: var(--accent);
}

.activity-label {
  flex: 1;
}

.activity-done {
  color: var(--success);
}

.thinking {
  display: flex;
  align-items: center;
  gap: 8px;
}

.turn-error {
  margin-top: 8px;
  font-size: 13px;
  color: var(--danger);
}

.turn-notice {
  margin-bottom: 8px;
  font-size: 12px;
  color: var(--accent);
  background: #fff;
  border: 1px dashed var(--border);
  border-radius: 8px;
  padding: 6px 10px;
}

.composer {
  border-top: 1px solid var(--border);
  padding: 12px 16px;
  display: flex;
  gap: 10px;
  align-items: flex-end;
}

.followups {
  padding: 0 16px 10px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  border-top: 1px dashed var(--border);
  padding-top: 10px;
}

.followup {
  cursor: pointer;
  border: 1px solid var(--border);
  background: var(--surface-muted);
  border-radius: 999px;
  font-size: 12px;
  padding: 4px 12px;
  white-space: normal;
}

.followup:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.feedback-row {
  display: flex;
  gap: 4px;
  margin-top: 8px;
}

.feedback-btn {
  border: none;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 13px;
  padding: 2px 6px;
  border-radius: 6px;
}

.feedback-btn:hover {
  background: var(--surface-muted);
}

.feedback-btn.active {
  color: var(--accent);
  background: var(--surface-muted);
}

.composer :deep(textarea) {
  font-size: 14px;
}
</style>
