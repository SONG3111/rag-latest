<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { SendOutlined, StopOutlined, ToolOutlined } from '@ant-design/icons-vue'
import { useNewsStore } from '@/stores/news'
import MarkdownContent from './MarkdownContent.vue'

defineProps<{ embedded?: boolean }>()

const store = useNewsStore()
const draft = ref('')
const scroller = ref<HTMLElement | null>(null)
const composing = ref(false)
const composingText = ref('')
let clearDuringComposition = false
let sentWhileComposing: string | null = null

const examples = [
  '今天科技领域最重要的事件是什么？',
  '这些报道有哪些来源互相矛盾？',
  '帮我梳理能源和汽车新闻的共同影响',
]

const composerText = computed(() => (composing.value ? composingText.value : draft.value))

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

function onCompositionEnd(event: CompositionEvent) {
  if (!composing.value) return
  composing.value = false
  const value = (event.target as HTMLTextAreaElement).value
  if (clearDuringComposition && value === sentWhileComposing) {
    draft.value = ''
  } else {
    draft.value = value
  }
  clearDuringComposition = false
  sentWhileComposing = null
  composingText.value = ''
}

function onPressEnter(event: KeyboardEvent) {
  if (composing.value || event.isComposing || event.keyCode === 229) return
  if (!event.shiftKey) {
    event.preventDefault()
    void send()
  }
}

async function scrollToBottom() {
  await nextTick()
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight
}

watch(
  () => store.turns.map((turn) => `${turn.content.length}:${turn.activities.length}`).join('|'),
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
  <aside class="news-chat-panel" :class="{ embedded }">
    <div v-if="!embedded" class="panel-header">
      <span>继续追问</span>
      <span v-if="store.streaming" class="streaming" aria-live="polite">检索中…</span>
    </div>

    <div ref="scroller" class="panel-body chat-scroller">
      <div v-if="store.turns.length === 0" class="chat-empty">
        <div class="chat-empty-title">从今日事件继续挖掘</div>
        <p>
          第一次提问会按左侧行业实时生成今日事件；15 分钟内追问复用缓存，
          不足时再定向补搜。回答保留媒体、日期与原文链接。
        </p>
        <button
          v-for="example in examples"
          :key="example"
          type="button"
          class="example-chip"
          @click="send(example)"
        >
          {{ example }}
        </button>
      </div>

      <div
        v-for="turn in store.turns"
        :key="turn.id"
        class="chat-turn"
        :class="turn.role"
      >
        <div class="chat-bubble">
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
              <span v-else class="activity-done">完成</span>
            </div>
          </div>
          <MarkdownContent
            v-if="turn.content && turn.role === 'assistant'"
            :text="turn.content"
          />
          <div v-else-if="turn.content" class="plain-content" v-text="turn.content" />
          <div v-if="turn.streaming && !turn.content" class="thinking">
            <a-spin size="small" /> 正在查找来源…
          </div>
          <div v-if="turn.error" class="turn-error" role="alert">{{ turn.error }}</div>
        </div>
      </div>
    </div>

    <div class="news-composer">
      <a-textarea
        :value="draft"
        :auto-size="{ minRows: 2, maxRows: 5 }"
        aria-label="新闻追问内容"
        name="news-followup"
        autocomplete="off"
        placeholder="追问今日事件、对比来源或查找后续报道…"
        :disabled="store.streaming"
        @update:value="onDraftInput"
        @compositionstart="onCompositionStart"
        @compositionend="onCompositionEnd"
        @press-enter="onPressEnter"
      />
      <div class="composer-actions">
        <span class="composer-hint">Enter 发送 · Shift+Enter 换行</span>
        <a-button v-if="store.streaming" danger size="small" @click="store.stop()">
          <template #icon><StopOutlined /></template>
          停止
        </a-button>
        <a-button
          v-else
          type="primary"
          size="small"
          :disabled="!composerText.trim()"
          @click="send()"
        >
          <template #icon><SendOutlined /></template>
          发送
        </a-button>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.news-chat-panel {
  display: flex;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
  background: var(--surface);
  border-inline-start: 1px solid var(--border);
}

.news-chat-panel.embedded {
  height: calc(100vh - 48px);
  border-inline-start: 0;
}

.streaming {
  color: var(--accent);
  font-size: 12px;
  font-weight: 400;
}

.chat-scroller {
  padding: 16px;
}

.chat-empty {
  padding: 18px 4px;
  color: var(--text-muted);
  font-size: 13px;
  line-height: 1.75;
}

.chat-empty-title {
  color: var(--text);
  font-size: 15px;
  font-weight: 650;
}

.chat-empty p {
  margin: 7px 0 16px;
}

.example-chip {
  display: block;
  width: 100%;
  margin: 8px 0 0;
  padding: 9px 11px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--surface-muted);
  color: var(--text);
  cursor: pointer;
  font: inherit;
  font-size: 12px;
  line-height: 1.55;
  text-align: start;
  transition: border-color 140ms ease, background-color 140ms ease;
}

.example-chip:hover {
  border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
  background: color-mix(in srgb, var(--accent) 5%, var(--surface));
}

.example-chip:active {
  scale: 0.98;
}

.chat-turn {
  display: flex;
  margin-bottom: 14px;
}

.chat-turn.user {
  justify-content: flex-end;
}

.chat-bubble {
  max-width: 92%;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 11px;
  background: var(--surface-muted);
  font-size: 13px;
  line-height: 1.7;
}

.chat-turn.assistant .chat-bubble {
  max-width: 100%;
  background: var(--surface);
}

.plain-content {
  white-space: pre-wrap;
}

.activities {
  margin-bottom: 7px;
}

.activity {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 0;
  color: var(--text-muted);
  font-size: 11px;
}

.activity.done,
.activity-done {
  color: var(--success);
}

.thinking {
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--text-muted);
}

.turn-error {
  margin-top: 5px;
  color: var(--danger);
  font-size: 12px;
}

.news-composer {
  padding: 12px;
  border-top: 1px solid var(--border);
  background: var(--surface);
}

.news-composer :deep(textarea) {
  resize: none;
}

.composer-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 8px;
}

.composer-hint {
  color: var(--text-muted);
  font-size: 10px;
}
</style>
