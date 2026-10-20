<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { message, Modal } from 'ant-design-vue'
import {
  AudioOutlined,
  CopyOutlined,
  DeleteOutlined,
  DownloadOutlined,
  PlusOutlined,
  ReloadOutlined,
  StopOutlined,
} from '@ant-design/icons-vue'
import { api } from '@/api'
import { useMeetingStore } from '@/stores/meeting'
import { useMeetingRecorder } from '@/composables/useMeetingRecorder'
import MarkdownContent from './MarkdownContent.vue'
import MeetingChatPanel from './MeetingChatPanel.vue'

const store = useMeetingStore()
const newLanguage = ref<'zh' | 'en' | 'auto'>('zh')
const notesDraft = ref('')
const titleDraft = ref('')
const languageDraft = ref<'zh' | 'en' | 'auto'>('zh')
const activeTab = ref('summary')
const generating = ref(false)
let notesSaveTimer: number | null = null

const recorder = useMeetingRecorder({
  onSegments: (segments) => store.applyLiveSegments(segments),
  onPartial: (text) => {
    store.partialTranscript = text
  },
  onRecorded: async () => {
    await finishRecording()
  },
  onFailed: async (text) => {
    message.error(text)
    await store.refreshCurrent().catch(() => undefined)
  },
})

const current = computed(() => store.current)
const isRecording = computed(() => recorder.recording.value)
const canStart = computed(
  () =>
    Boolean(current.value) &&
    current.value?.status === 'draft' &&
    store.health?.enabled !== false &&
    !recorder.starting.value,
)
const chatUnavailableMessage = computed(() => {
  if (isRecording.value) return '录音进行中，停止录音后即可基于转写内容追问。'
  if (!current.value?.segments.length) return '当前还没有转写内容，录音完成后再来追问。'
  return ''
})

watch(
  () => current.value?.id,
  () => {
    notesDraft.value = current.value?.notes || ''
    titleDraft.value = current.value?.title || ''
    languageDraft.value = (current.value?.language || 'zh') as 'zh' | 'en' | 'auto'
    activeTab.value = current.value?.summary_markdown ? 'summary' : 'transcript'
  },
  { immediate: true },
)

watch(notesDraft, (value) => {
  if (!current.value) return
  if (notesSaveTimer !== null) window.clearTimeout(notesSaveTimer)
  notesSaveTimer = window.setTimeout(() => {
    void store.saveNotes(value).catch((error) => {
      message.error(error instanceof Error ? error.message : String(error))
    })
  }, 800)
})

onMounted(() => {
  void store.bootstrap()
})

onBeforeUnmount(() => {
  if (notesSaveTimer !== null) window.clearTimeout(notesSaveTimer)
})

function statusColor(status?: string): string {
  if (status === 'ready' || status === 'recorded') return 'green'
  if (status === 'recording') return 'red'
  if (status === 'generating') return 'blue'
  if (status === 'failed') return 'red'
  if (status === 'interrupted') return 'orange'
  return 'default'
}

function statusText(status?: string): string {
  const labels: Record<string, string> = {
    draft: '待录音',
    recording: '录音中',
    recorded: '已录音',
    interrupted: '录音中断',
    failed: '失败',
    none: '未生成',
    generating: '生成中',
    ready: '已完成',
  }
  return labels[status || ''] || status || '未知'
}

function formatClock(totalSeconds: number): string {
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  return [hours, minutes, seconds].map((item) => String(item).padStart(2, '0')).join(':')
}

function formatTimestamp(ms: number): string {
  return formatClock(Math.max(0, Math.floor(ms / 1000)))
}

function formatDate(value: string | null): string {
  if (!value) return '未开始'
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

async function createMeeting() {
  try {
    await store.create('', newLanguage.value)
    notesDraft.value = ''
    titleDraft.value = current.value?.title || ''
    languageDraft.value = newLanguage.value
    activeTab.value = 'transcript'
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}

async function openMeeting(meetingId: string) {
  if (isRecording.value) {
    message.warning('请先停止当前录音')
    return
  }
  try {
    await store.select(meetingId)
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}

async function startRecording() {
  if (!current.value || !canStart.value) return
  await store.saveNotes(notesDraft.value)
  await store.startRecordingState()
  activeTab.value = 'transcript'
  await recorder.start(current.value.id)
  if (recorder.error.value && !recorder.recording.value) {
    await store.refreshCurrent().catch(() => undefined)
  }
}

async function stopRecording() {
  if (current.value) {
    await store.saveNotes(notesDraft.value).catch((error) => {
      message.error(error instanceof Error ? error.message : String(error))
    })
  }
  await recorder.stop()
}

async function finishRecording() {
  generating.value = true
  activeTab.value = 'summary'
  try {
    await store.finishRecordingState()
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    generating.value = false
    await store.refreshHealth()
  }
}

async function saveNotes() {
  if (!current.value) return
  try {
    await store.saveNotes(notesDraft.value)
    message.success('笔记已保存')
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}

async function saveTitle() {
  if (!current.value) return
  const title = titleDraft.value.trim()
  if (!title || title === current.value.title) return
  try {
    await store.update({ title })
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}

async function saveLanguage() {
  if (!current.value || current.value.status !== 'draft') return
  await store.update({ language: languageDraft.value }).catch((error) => {
    message.error(error instanceof Error ? error.message : String(error))
  })
}

async function regenerate() {
  if (!current.value || generating.value) return
  generating.value = true
  try {
    await store.saveNotes(notesDraft.value)
    await store.generateMinutes()
    await store.refreshCurrent()
    message.success('纪要已生成')
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    generating.value = false
  }
}

async function copyMinutes() {
  if (!current.value?.summary_markdown) return
  await navigator.clipboard.writeText(current.value.summary_markdown)
  message.success('纪要已复制')
}

function downloadMinutes() {
  if (!current.value) return
  const link = document.createElement('a')
  link.href = api.meetingExportUrl(current.value.id)
  link.download = ''
  link.click()
}

function removeMeeting() {
  if (!current.value) return
  Modal.confirm({
    title: '删除这场会议？',
    content: '转写和纪要会一起删除，无法恢复。',
    okText: '删除',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      await store.remove(current.value!.id)
    },
  })
}
</script>

<template>
  <div class="meeting-shell">
    <aside class="panel meeting-side">
      <div class="panel-header">
        <span>会议记录</span>
        <a-button type="text" size="small" title="刷新" @click="store.bootstrap">
          <template #icon><ReloadOutlined /></template>
        </a-button>
      </div>
      <div class="new-meeting">
        <a-select v-model:value="newLanguage" size="small" style="width: 94px">
          <a-select-option value="zh">中文</a-select-option>
          <a-select-option value="en">English</a-select-option>
          <a-select-option value="auto">自动</a-select-option>
        </a-select>
        <a-button type="primary" size="small" @click="createMeeting">
          <template #icon><PlusOutlined /></template>
          新建会议
        </a-button>
      </div>
      <div class="panel-body meeting-list">
        <button
          v-for="meeting in store.meetings"
          :key="meeting.id"
          class="meeting-item"
          :class="{ active: current?.id === meeting.id }"
          @click="openMeeting(meeting.id)"
        >
          <span class="meeting-title">{{ meeting.title }}</span>
          <span class="meeting-meta">
            <a-tag :color="statusColor(meeting.status)" size="small">
              {{ statusText(meeting.status) }}
            </a-tag>
            <span>{{ formatDate(meeting.started_at) }}</span>
          </span>
        </button>
        <div v-if="!store.meetings.length && !store.loading" class="empty-mini">
          还没有会议记录
        </div>
      </div>
      <div class="meeting-health">
        <div class="health-line">
          <span>本地转写</span>
          <a-tag
            :color="store.health?.ready ? 'green' : store.health?.loading ? 'blue' : 'default'"
            size="small"
          >
            {{
              store.health?.enabled === false
                ? '未启用'
                : store.health?.ready
                  ? '已加载'
                  : store.health?.loading
                    ? '加载中'
                    : '首次录音加载'
            }}
          </a-tag>
        </div>
        <div class="health-model">
          {{ store.health?.model || 'small' }} · {{ store.health?.device || 'auto' }}
        </div>
        <div v-if="store.health?.error" class="health-error">{{ store.health.error }}</div>
      </div>
    </aside>

    <main class="panel meeting-main">
      <div v-if="!current" class="empty meeting-empty">
        <div class="empty-title">会议助手</div>
        <div>新建一场会议，同时录制麦克风和系统声音。停止后会生成可追问、可导出的会议纪要。</div>
      </div>

      <template v-else>
        <div class="meeting-header">
          <div class="title-area">
            <a-input
              v-model:value="titleDraft"
              class="title-input"
              :disabled="isRecording"
              @blur="saveTitle"
              @press-enter="saveTitle"
            />
            <div class="meeting-sub">
              <span>{{ formatDate(current.started_at) }}</span>
              <span v-if="current.duration_seconds">
                时长 {{ formatClock(current.duration_seconds) }}
              </span>
            </div>
          </div>
          <div class="header-actions">
            <a-tag :color="statusColor(current.status)" size="small">
              {{ statusText(current.status) }}
            </a-tag>
            <a-tag :color="statusColor(current.summary_status)" size="small">
              纪要：{{ statusText(current.summary_status) }}
            </a-tag>
            <a-button
              v-if="current.summary_markdown"
              size="small"
              @click="copyMinutes"
            >
              <template #icon><CopyOutlined /></template>
              复制
            </a-button>
            <a-button
              v-if="current.summary_markdown || current.segments.length"
              size="small"
              @click="downloadMinutes"
            >
              <template #icon><DownloadOutlined /></template>
              导出
            </a-button>
            <a-button danger size="small" :disabled="isRecording" @click="removeMeeting">
              <template #icon><DeleteOutlined /></template>
            </a-button>
          </div>
        </div>

        <div class="privacy-notice">
          音频只在本机内存中流转，不保存原始录音；生成纪要和追问时，转写文本会发送给当前配置的语言模型。
        </div>

        <div class="record-bar">
          <template v-if="recorder.starting.value || recorder.loadingModel.value">
            <a-spin size="small" />
            <span class="record-hint">
              正在准备本地转写模型…首次使用需要下载模型，请稍候。
            </span>
          </template>
          <template v-else-if="current.status === 'draft' && !isRecording">
            <a-select
              v-model:value="languageDraft"
              size="small"
              style="width: 100px"
              @change="saveLanguage"
            >
              <a-select-option value="zh">中文</a-select-option>
              <a-select-option value="en">English</a-select-option>
              <a-select-option value="auto">自动识别</a-select-option>
            </a-select>
            <a-button
              type="primary"
              :loading="recorder.starting.value"
              :disabled="!canStart"
              @click="startRecording"
            >
              <template #icon><AudioOutlined /></template>
              开始录音
            </a-button>
            <span class="record-hint">
              麦克风录你的声音，系统声音录会议中其他人的声音。请共享屏幕/标签页并勾选“分享系统音频”；使用外放时建议戴耳机，避免回录。
            </span>
          </template>
          <template v-else-if="isRecording">
            <span class="recording-dot" />
            <strong class="recording-clock">{{ formatClock(recorder.elapsedSeconds.value) }}</strong>
            <span class="record-source">{{ recorder.sourceLabel.value }}</span>
            <span
              v-if="recorder.computeLagSeconds.value >= 2"
              class="lag-indicator"
              :class="{ warning: recorder.computeLagSeconds.value >= 10 }"
            >
              处理延迟 {{ recorder.computeLagSeconds.value.toFixed(1) }} 秒
            </span>
            <a-button
              danger
              :loading="recorder.stopping.value"
              @click="stopRecording"
            >
              <template #icon><StopOutlined /></template>
              停止并生成纪要
            </a-button>
          </template>
          <template v-else>
            <span class="record-hint">
              {{ current.error || '本场录音已结束。可以修改笔记后重新生成纪要。' }}
            </span>
            <a-button
              :loading="generating"
              :disabled="
                current.summary_status === 'generating' ||
                (!current.segments.length && !notesDraft.trim())
              "
              @click="regenerate"
            >
              重新生成纪要
            </a-button>
          </template>
        </div>

        <div class="meeting-workspace">
          <div class="meeting-content">
            <a-tabs v-model:active-key="activeTab">
              <a-tab-pane key="summary" tab="会议纪要">
                <div v-if="generating || current.summary_status === 'generating'" class="summary-loading">
                  <a-spin size="small" />
                  <span>正在根据转写生成纪要，长会议可能需要几分钟…</span>
                </div>
                <MarkdownContent
                  v-else-if="current.summary_markdown"
                  class="summary-content"
                  :text="current.summary_markdown"
                />
                <div v-else class="empty-mini">
                  停止录音后会自动生成；也可以先补充笔记再手动生成。
                </div>
              </a-tab-pane>

              <a-tab-pane key="transcript" tab="实时转写">
                <div class="transcript-list">
                  <div
                    v-for="segment in current.segments"
                    :key="segment.id"
                    class="transcript-line"
                  >
                    <span class="stamp">{{ formatTimestamp(segment.start_ms) }}</span>
                    <span>{{ segment.text }}</span>
                  </div>
                  <div v-if="store.partialTranscript" class="transcript-line partial">
                    <span class="stamp">识别中</span>
                    <span>{{ store.partialTranscript }}</span>
                  </div>
                  <div v-if="!current.segments.length && !store.partialTranscript" class="empty-mini">
                    {{ isRecording ? '正在等待语音…' : '还没有转写内容' }}
                  </div>
                </div>
              </a-tab-pane>

              <a-tab-pane key="notes" tab="我的笔记">
                <a-textarea
                  v-model:value="notesDraft"
                  :auto-size="{ minRows: 12, maxRows: 30 }"
                  placeholder="会前议程、会中速记、需要重点保留的内容…"
                  @blur="saveNotes"
                />
                <div class="notes-actions">
                  <span>{{ store.savingNotes ? '保存中…' : '笔记会参与纪要生成' }}</span>
                  <a-button size="small" :loading="store.savingNotes" @click="saveNotes">
                    保存笔记
                  </a-button>
                </div>
              </a-tab-pane>
            </a-tabs>
          </div>

          <MeetingChatPanel
            v-if="current.id"
            :meeting-id="current.id"
            :disabled="isRecording || !current.segments.length"
            :unavailable-message="chatUnavailableMessage"
          />
        </div>
      </template>
    </main>
  </div>
</template>

<style scoped>
.meeting-shell {
  display: grid;
  grid-template-columns: 290px minmax(0, 1fr);
  height: calc(100vh - 48px);
  overflow: hidden;
  background: var(--surface-muted);
}

.meeting-side {
  border-right: 1px solid var(--border);
}

.new-meeting {
  padding: 12px;
  display: flex;
  gap: 8px;
  border-bottom: 1px solid var(--border);
}

.meeting-list {
  padding: 8px;
}

.meeting-item {
  width: 100%;
  border: 1px solid transparent;
  background: transparent;
  text-align: left;
  border-radius: 9px;
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 7px;
  cursor: pointer;
}

.meeting-item:hover {
  background: var(--surface-muted);
}

.meeting-item.active {
  background: #edf4ff;
  border-color: #c9ddff;
}

.meeting-title {
  font-size: 13px;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.meeting-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--text-muted);
  font-size: 10px;
}

.meeting-health {
  padding: 12px;
  border-top: 1px solid var(--border);
  font-size: 11px;
}

.health-line {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.health-model {
  color: var(--text-muted);
  margin-top: 6px;
}

.health-error {
  color: var(--danger);
  margin-top: 6px;
  word-break: break-word;
}

.meeting-main {
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}

.meeting-empty {
  margin: auto;
}

.empty-title {
  font-size: 18px;
  font-weight: 600;
  margin-bottom: 8px;
  color: var(--text);
}

.meeting-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 20px 12px;
  flex: 0 0 auto;
}

.title-area {
  min-width: 220px;
  flex: 1;
}

.title-input {
  font-size: 18px;
  font-weight: 600;
  border-color: transparent;
  padding-left: 0;
}

.title-input:hover,
.title-input:focus {
  border-color: var(--border);
  padding-left: 8px;
}

.meeting-sub {
  display: flex;
  gap: 14px;
  color: var(--text-muted);
  font-size: 11px;
  margin-top: 7px;
}

.header-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 7px;
  max-width: 52%;
}

.privacy-notice {
  margin: 0 20px 10px;
  padding: 8px 10px;
  border-radius: 8px;
  background: #fffdf7;
  color: var(--text-muted);
  font-size: 11px;
  flex: 0 0 auto;
}

.record-bar {
  margin: 0 20px 14px;
  min-height: 46px;
  padding: 9px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  display: flex;
  align-items: center;
  gap: 10px;
  background: var(--surface);
  flex: 0 0 auto;
}

.record-hint {
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.5;
}

.recording-dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--danger);
  box-shadow: 0 0 0 4px rgba(220, 38, 38, 0.12);
}

.recording-clock {
  font-variant-numeric: tabular-nums;
  font-size: 16px;
}

.record-source {
  color: var(--text-muted);
  font-size: 12px;
  flex: 1;
}

.lag-indicator {
  color: var(--text-muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.lag-indicator.warning {
  color: var(--warning);
}

.meeting-workspace {
  margin: 0 20px 20px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--surface);
  overflow: hidden;
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) minmax(340px, 0.85fr);
}

.meeting-content {
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  padding: 0 18px 18px;
}

.summary-content {
  font-size: 13px;
  line-height: 1.75;
}

.summary-loading {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 18px 4px;
  color: var(--text-muted);
  font-size: 12px;
}

.transcript-list {
  padding: 4px 0;
}

.transcript-line {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr);
  gap: 10px;
  padding: 7px 0;
  border-bottom: 1px dashed var(--border);
  font-size: 13px;
  line-height: 1.65;
}

.transcript-line.partial {
  color: var(--text-muted);
}

.stamp {
  color: var(--text-muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  padding-top: 2px;
}

.notes-actions {
  margin-top: 10px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  color: var(--text-muted);
  font-size: 11px;
}

.empty-mini {
  padding: 12px 0;
  color: var(--text-muted);
  font-size: 12px;
}

@media (max-width: 1000px) {
  .meeting-shell {
    grid-template-columns: 240px minmax(0, 1fr);
  }

  .meeting-header {
    flex-direction: column;
  }

  .header-actions {
    max-width: 100%;
    justify-content: flex-start;
  }

  .meeting-workspace {
    grid-template-columns: minmax(0, 1fr) minmax(300px, 0.85fr);
  }
}
</style>
