<script setup lang="ts">
import { computed } from 'vue'
import type { CitationPreview } from '@/types'

/**
 * Modal that renders the original-text window behind one citation.
 *
 * The backend re-reads the file live through the read-only MCP tools and marks
 * which row/paragraph the citation pointed at; this component only renders that
 * window — three shapes, one per source kind (excel sheet window, word
 * paragraphs, word table).
 */
const props = defineProps<{
  open: boolean
  loading: boolean
  preview: CitationPreview | null
  error: string | null
}>()

const emit = defineEmits<{ (e: 'close'): void }>()

const title = computed(() => {
  const preview = props.preview
  if (!preview) return '原文预览'
  return `${preview.file} · ${preview.location}`
})

function cellText(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'object') {
    const record = value as Record<string, unknown>
    return String(record.cached_value ?? record.formula ?? '')
  }
  return String(value)
}

function isHighlighted(index: number): boolean {
  return props.preview?.highlight === index
}
</script>

<template>
  <a-modal
    :open="open"
    :title="title"
    :footer="null"
    :width="640"
    @cancel="emit('close')"
  >
    <div v-if="loading" class="preview-loading"><a-spin /> 正在读取原文…</div>
    <div v-else-if="error" class="preview-error">{{ error }}</div>
    <template v-else-if="preview">
      <!-- Excel：引用行前后各若干行的窗口 -->
      <table v-if="preview.kind === 'excel'" class="preview-table">
        <tbody>
          <tr>
            <td class="row-number"></td>
            <td v-for="(_, columnIndex) in preview.rows?.[0] ?? []" :key="columnIndex" class="col-letter">
              {{ String.fromCharCode(65 + columnIndex) }}
            </td>
          </tr>
          <tr
            v-for="(row, rowIndex) in preview.rows"
            :key="rowIndex"
            :class="{ highlighted: isHighlighted(rowIndex) }"
          >
            <td class="row-number">{{ (preview.start_row ?? 1) + rowIndex }}</td>
            <td v-for="(cell, columnIndex) in row" :key="columnIndex">{{ cellText(cell) }}</td>
          </tr>
        </tbody>
      </table>

      <!-- Word 表格 -->
      <table v-else-if="preview.kind === 'word_table'" class="preview-table">
        <tbody>
          <tr
            v-for="(row, rowIndex) in preview.rows"
            :key="rowIndex"
            :class="{ highlighted: isHighlighted(rowIndex) }"
          >
            <td v-for="(cell, columnIndex) in row" :key="columnIndex">{{ cellText(cell) }}</td>
          </tr>
        </tbody>
      </table>

      <!-- Word 正文段落 -->
      <div v-else class="preview-paragraphs">
        <p
          v-for="paragraph in preview.paragraphs"
          :key="paragraph.index"
          :class="{ highlighted: isHighlighted(paragraph.index), heading: paragraph.is_heading }"
        >
          {{ paragraph.text }}
        </p>
      </div>

      <div class="preview-note muted">内容为按当前文件实时读取，黄色为引用所指位置。</div>
    </template>
  </a-modal>
</template>

<style scoped>
.preview-loading {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 20px 0;
}

.preview-error {
  color: var(--danger);
  font-size: 13px;
  padding: 12px 0;
}

.preview-table {
  border-collapse: collapse;
  width: 100%;
  font-size: 12px;
}

.preview-table td {
  border: 1px solid var(--border);
  padding: 5px 9px;
  max-width: 180px;
  overflow-wrap: break-word;
}

.preview-table .row-number,
.preview-table .col-letter {
  color: var(--text-muted);
  background: var(--surface-muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  text-align: center;
  white-space: nowrap;
}

tr.highlighted td,
p.highlighted {
  background: #fef9c3;
}

p.highlighted {
  outline: 1px solid #fde047;
  border-radius: 3px;
}

.preview-paragraphs {
  max-height: 420px;
  overflow-y: auto;
}

.preview-paragraphs p {
  padding: 5px 8px;
  line-height: 1.8;
  border-radius: 3px;
}

.preview-paragraphs p.heading {
  font-weight: 600;
}

.preview-note {
  margin-top: 12px;
  font-size: 11px;
}
</style>
