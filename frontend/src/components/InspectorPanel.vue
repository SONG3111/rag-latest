<script setup lang="ts">
import { computed, ref } from 'vue'
import { DeleteOutlined, EyeOutlined, SafetyCertificateOutlined, ToolOutlined } from '@ant-design/icons-vue'
import { useWorkspaceStore } from '@/stores/workspace'
import { api } from '@/api'
import type { Citation, CitationPreview } from '@/types'
import OperationCard from './OperationCard.vue'
import CitationPreviewModal from './CitationPreviewModal.vue'

const store = useWorkspaceStore()

// 引用点击预览：按 citation 的 file + location 实时读一次原文窗口。
const previewOpen = ref(false)
const previewLoading = ref(false)
const previewData = ref<CitationPreview | null>(null)
const previewError = ref<string | null>(null)

async function openPreview(citation: Citation) {
  if (!store.activeWorkspaceId || !citation.file || !citation.location) return
  previewOpen.value = true
  previewLoading.value = true
  previewError.value = null
  previewData.value = null
  try {
    previewData.value = await api.previewCitation(
      store.activeWorkspaceId,
      citation.file,
      citation.location,
    )
  } catch (error) {
    previewError.value = error instanceof Error ? error.message : String(error)
  } finally {
    previewLoading.value = false
  }
}

// 失败的提案也留在待确认面板：失败多为瞬时原因（文件被占用、校验冲突），
// 排除后用户可以直接点「重试」。
const pending = computed(() =>
  store.operations.filter((item) => item.status === 'proposed' || item.status === 'failed'),
)
const history = computed(() =>
  store.operations.filter((item) => item.status !== 'proposed' && item.status !== 'failed'),
)

const statusLabels: Record<string, string> = {
  proposed: '待确认',
  applied: '已应用',
  rejected: '已放弃',
  failed: '失败',
}

const gatedTools = computed(() => store.tools.filter((tool) => tool.requires_approval))
const freeTools = computed(() => store.tools.filter((tool) => !tool.requires_approval))

/**
 * Band a cross-encoder relevance score.
 *
 * The bands come from the calibration recorded in the README: in-corpus hits score
 * around 0.9, a topically-adjacent but non-answering passage sits near 0.45, and a
 * question the corpus cannot answer stays under 0.03.
 */
function scoreBand(score: number): string {
  if (score >= 0.5) return 'high'
  if (score >= 0.15) return 'medium'
  return 'low'
}
</script>

<template>
  <aside class="panel inspector">
    <a-tabs size="small" class="inspector-tabs">
      <a-tab-pane key="pending">
        <template #tab>
          <span>待确认<template v-if="pending.length"> ({{ pending.length }})</template></span>
        </template>
        <div class="tab-body">
          <div v-if="pending.length === 0" class="empty">
            没有待确认的修改。<br />
            让 Agent 修改文件时，变更预览会出现在这里。
          </div>
          <OperationCard v-for="item in pending" :key="item.id" :operation="item" compact />

          <template v-if="history.length > 0">
            <div class="section-title">操作记录</div>
            <div v-for="item in history" :key="item.id" class="history-item">
              <div class="history-main">
                <span class="history-status" :class="item.status">
                  {{ statusLabels[item.status] ?? item.status }}
                </span>
                <span class="history-summary">{{ item.summary }}</span>
              </div>
              <a-button v-if="item.status === 'applied'" type="link" size="small" @click="store.revertOperation(item.id)">
                还原
              </a-button>
            </div>
          </template>
        </div>
      </a-tab-pane>

      <a-tab-pane key="citations">
        <template #tab><span>引用来源</span></template>
        <div class="tab-body">
          <div v-if="store.latestCitations.length === 0" class="empty">
            还没有引用。<br />向知识库提问后，出处会显示在这里。
          </div>
          <div v-for="(citation, index) in store.latestCitations" :key="index" class="citation">
            <div class="citation-head">
              <span class="citation-index">[{{ index + 1 }}]</span>
              <span class="citation-file" :title="citation.file ?? ''">{{ citation.file }}</span>
            </div>
            <div class="citation-location muted">{{ citation.location }}</div>
            <div class="citation-snippet">{{ citation.snippet }}</div>
            <div class="citation-score">
              <template v-if="citation.score !== null && citation.score !== undefined">
                <span
                  class="score-badge"
                  :class="citation.score_source === 'rerank' ? scoreBand(citation.score) : 'ordinal'"
                >
                  {{ citation.score_source === 'rerank' ? '相关性' : '排序分' }}
                  {{ citation.score.toFixed(3) }}
                </span>
                <span v-if="citation.score_source === 'rerank' && citation.score < 0.15" class="muted">
                  低于阈值，仅供参考
                </span>
              </template>
              <a-button
                v-if="citation.file && citation.location"
                type="link"
                size="small"
                class="preview-button"
                @click="openPreview(citation)"
              >
                <EyeOutlined /> 查看原文
              </a-button>
            </div>
          </div>
        </div>
      </a-tab-pane>

      <a-tab-pane key="tools">
        <template #tab>
          <ToolOutlined />
        </template>
        <div class="tab-body">
          <div class="section-title">
            <SafetyCertificateOutlined /> 需确认后执行（{{ gatedTools.length }}）
          </div>
          <div v-for="tool in gatedTools" :key="tool.name" class="tool-row">
            <code>{{ tool.name }}</code>
            <span class="tool-badge danger">需审批</span>
          </div>

          <div class="section-title">可直接调用（{{ freeTools.length }}）</div>
          <div v-for="tool in freeTools" :key="tool.name" class="tool-row">
            <code>{{ tool.name }}</code>
            <span class="tool-badge safe">只读</span>
          </div>

          <div class="tool-note muted">
            这些工具由独立的 MCP Server 通过 stdio 提供，Agent 会根据你的问题自行决定调用哪个。
          </div>
        </div>
      </a-tab-pane>
    </a-tabs>

    <CitationPreviewModal
      :open="previewOpen"
      :loading="previewLoading"
      :preview="previewData"
      :error="previewError"
      @close="previewOpen = false"
    />
  </aside>
</template>

<style scoped>
.inspector-tabs {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.inspector-tabs :deep(.ant-tabs-nav) {
  margin: 0;
  padding: 0 12px;
}

.inspector-tabs :deep(.ant-tabs-content-holder) {
  overflow: hidden;
}

.inspector-tabs :deep(.ant-tabs-content) {
  height: 100%;
}

.inspector-tabs :deep(.ant-tabs-tabpane) {
  height: 100%;
  overflow-y: auto;
}

.tab-body {
  padding: 14px;
}

.section-title {
  margin: 18px 0 8px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-muted);
  display: flex;
  align-items: center;
  gap: 6px;
}

.citation {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 10px 12px;
  margin-bottom: 10px;
  background: var(--surface-muted);
}

.citation-head {
  display: flex;
  gap: 6px;
  align-items: baseline;
  font-size: 13px;
  font-weight: 600;
}

.citation-index {
  color: var(--accent);
  font-family: ui-monospace, monospace;
}

.citation-file {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.citation-location {
  font-size: 12px;
  margin-top: 2px;
}

.citation-snippet {
  margin-top: 7px;
  font-size: 12px;
  line-height: 1.7;
  color: #374151;
  white-space: pre-wrap;
  max-height: 120px;
  overflow: hidden;
}

.citation-score {
  margin-top: 5px;
  font-size: 11px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.score-badge {
  padding: 1px 7px;
  border-radius: 999px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.score-badge.high {
  background: #f0fdf4;
  color: var(--success);
}

.score-badge.medium {
  background: #fffbeb;
  color: var(--warning);
}

.score-badge.low {
  background: #fef2f2;
  color: var(--danger);
}

.score-badge.ordinal {
  background: var(--surface-muted);
  color: var(--text-muted);
}

.preview-button {
  margin-left: auto;
  padding-left: 0;
  padding-right: 0;
  font-size: 11px;
}

.tool-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 0;
  font-size: 12px;
  border-bottom: 1px dashed var(--border);
}

code {
  font-size: 12px;
  color: #374151;
}

.tool-badge {
  font-size: 11px;
  padding: 1px 7px;
  border-radius: 999px;
  white-space: nowrap;
}

.tool-badge.danger {
  background: #fef2f2;
  color: var(--danger);
}

.tool-badge.safe {
  background: #f0fdf4;
  color: var(--success);
}

.tool-note {
  margin-top: 14px;
  font-size: 11px;
  line-height: 1.7;
}

.history-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 7px 0;
  border-bottom: 1px dashed var(--border);
  font-size: 12px;
}

.history-main {
  display: flex;
  gap: 6px;
  align-items: baseline;
  min-width: 0;
}

.history-status {
  flex-shrink: 0;
  font-size: 11px;
}

.history-status.applied {
  color: var(--success);
}

.history-status.rejected {
  color: var(--text-muted);
}

.history-status.failed {
  color: var(--danger);
}

.history-summary {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-muted);
}
</style>
