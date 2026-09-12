<script setup lang="ts">
import { computed } from 'vue'
import { message } from 'ant-design-vue'
import {
  CheckOutlined,
  CloseOutlined,
  UndoOutlined,
  WarningOutlined,
} from '@ant-design/icons-vue'
import { useWorkspaceStore } from '@/stores/workspace'
import type { Operation } from '@/types'

const props = defineProps<{ operation: Operation; compact?: boolean }>()
const store = useWorkspaceStore()

const isPending = computed(() => props.operation.status === 'proposed')
const isFailed = computed(() => props.operation.status === 'failed')
const isRetryable = computed(() => isPending.value || isFailed.value)
const isApplied = computed(() => props.operation.status === 'applied')

function render(value: unknown): string {
  if (value === null || value === undefined || value === '') return '（空）'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

async function apply() {
  try {
    await store.applyOperation(props.operation.id)
    message.success('修改已写入文件')
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}

async function reject() {
  try {
    await store.rejectOperation(props.operation.id)
    message.info('已放弃该修改，文件未变动')
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}

async function revert() {
  try {
    await store.revertOperation(props.operation.id)
    message.success('已还原到修改前的版本')
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}
</script>

<template>
  <div class="operation-card" :class="[operation.status, { compact }]">
    <div class="operation-head">
      <WarningOutlined v-if="isPending" class="icon pending" />
      <CheckOutlined v-else-if="isApplied" class="icon applied" />
      <CloseOutlined v-else class="icon rejected" />
      <div class="operation-title">{{ operation.summary }}</div>
    </div>

    <div v-if="operation.diff.length > 0" class="diff-table">
      <div class="diff-header">
        <span class="col-target">位置</span>
        <span class="col-before">修改前</span>
        <span class="col-after">修改后</span>
      </div>
      <div v-for="(entry, index) in operation.diff" :key="index" class="diff-row">
        <span class="col-target">{{ entry.cell ?? entry.location ?? `第 ${entry.row} 行` }}</span>
        <span class="col-before">{{ render(entry.before) }}</span>
        <span class="col-after">{{ render(entry.after) }}</span>
      </div>
    </div>

    <div v-else-if="isPending" class="muted small">
      该操作没有可预览的逐项变更，确认后将直接执行。
    </div>

    <div v-if="operation.error" class="operation-error">{{ operation.error }}</div>
    <div v-if="operation.backup_path" class="muted small backup">
      已备份原文件，可一键还原
    </div>

    <div v-if="isRetryable" class="operation-actions">
      <a-button type="primary" size="small" @click="apply">
        <template #icon><CheckOutlined /></template>
        {{ isFailed ? '重试' : '应用修改' }}
      </a-button>
      <a-button size="small" @click="reject">
        <template #icon><CloseOutlined /></template>
        放弃
      </a-button>
    </div>
    <div v-else-if="isApplied && operation.backup_path" class="operation-actions">
      <a-button size="small" @click="revert">
        <template #icon><UndoOutlined /></template>
        还原
      </a-button>
    </div>
  </div>
</template>

<style scoped>
.operation-card {
  border: 1px solid var(--border);
  border-left: 3px solid var(--warning);
  border-radius: var(--radius);
  padding: 12px 14px;
  margin-top: 10px;
  background: #fffdf7;
}

.operation-card.applied {
  border-left-color: var(--success);
  background: #f6fdf8;
}

.operation-card.rejected,
.operation-card.failed {
  border-left-color: var(--text-muted);
  background: var(--surface-muted);
}

.operation-card.compact {
  margin-top: 8px;
  padding: 10px 12px;
}

.operation-head {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 13px;
  font-weight: 500;
  line-height: 1.6;
}

.icon {
  margin-top: 3px;
}

.icon.pending {
  color: var(--warning);
}

.icon.applied {
  color: var(--success);
}

.icon.rejected {
  color: var(--text-muted);
}

.operation-title {
  flex: 1;
}

.diff-table {
  margin-top: 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
  font-size: 12px;
}

.diff-header,
.diff-row {
  display: grid;
  grid-template-columns: 88px 1fr 1fr;
  gap: 8px;
  padding: 6px 10px;
}

.diff-header {
  background: var(--surface-muted);
  font-weight: 600;
  color: var(--text-muted);
}

.diff-row + .diff-row {
  border-top: 1px solid var(--border);
}

.col-before {
  color: var(--danger);
  text-decoration: line-through;
  word-break: break-all;
}

.col-after {
  color: var(--success);
  font-weight: 500;
  word-break: break-all;
}

.col-target {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  color: var(--text-muted);
}

.operation-actions {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}

.operation-error {
  margin-top: 8px;
  font-size: 12px;
  color: var(--danger);
}

.small {
  font-size: 12px;
}

.backup {
  margin-top: 8px;
}
</style>
