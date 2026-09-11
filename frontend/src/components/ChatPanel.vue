<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { SendOutlined, ToolOutlined } from '@ant-design/icons-vue'
import { useWorkspaceStore } from '@/stores/workspace'
import MarkdownContent from './MarkdownContent.vue'
import OperationCard from './OperationCard.vue'

const store = useWorkspaceStore()
const draft = ref('')
const scroller = ref<HTMLElement | null>(null)

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
  const content = (text ?? draft.value).trim()
  if (!content || store.streaming) return
  if (!store.activeWorkspaceId) {
    message.warning('请先选择或新建一个工作区')
    return
  }
  draft.value = ''
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

          <MarkdownContent
            v-if="turn.content && turn.role === 'assistant'"
            class="content"
            :text="turn.content"
          />
          <div v-else-if="turn.content" class="content plain" v-text="turn.content" />

          <div v-if="turn.streaming && !turn.content" class="thinking muted">
            <a-spin size="small" /> 正在思考…
          </div>

          <div v-if="turn.error" class="turn-error">{{ turn.error }}</div>

          <OperationCard
            v-for="operation in turn.proposals"
            :key="operation.id"
            :operation="operation"
          />
        </div>
      </div>
    </div>

    <div class="composer">
      <a-textarea
        v-model:value="draft"
        :auto-size="{ minRows: 1, maxRows: 5 }"
        placeholder="描述你想查询或修改的内容，Enter 发送，Shift+Enter 换行"
        :disabled="store.streaming"
        @press-enter="
          (event: KeyboardEvent) => {
            if (!event.shiftKey) {
              event.preventDefault()
              send()
            }
          }
        "
      />
      <a-button
        type="primary"
        :disabled="!draft.trim() || store.streaming || !store.activeWorkspaceId"
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

.composer {
  border-top: 1px solid var(--border);
  padding: 12px 16px;
  display: flex;
  gap: 10px;
  align-items: flex-end;
}

.composer :deep(textarea) {
  font-size: 14px;
}
</style>
