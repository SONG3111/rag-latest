<script setup lang="ts">
import { computed, ref } from 'vue'
import { Modal, message } from 'ant-design-vue'
import {
  DeleteOutlined,
  DownloadOutlined,
  FileExcelOutlined,
  FileWordOutlined,
  InboxOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons-vue'
import { useWorkspaceStore } from '@/stores/workspace'
import { api } from '@/api'

const store = useWorkspaceStore()
const creating = ref(false)
const newName = ref('')
const uploading = ref(false)

const files = computed(() => store.files)

function statusColor(status: string): string {
  if (status === 'indexed') return 'success'
  if (status === 'failed') return 'error'
  if (status === 'indexing') return 'processing'
  return 'default'
}

function statusLabel(status: string): string {
  return { indexed: '已索引', failed: '失败', indexing: '索引中', pending: '待索引' }[status] ?? status
}

async function createWorkspace() {
  if (!newName.value.trim()) return
  try {
    await store.createWorkspace(newName.value.trim())
    newName.value = ''
    creating.value = false
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}

async function handleUpload(event: Event) {
  const input = event.target as HTMLInputElement
  const selected = Array.from(input.files ?? [])
  if (selected.length === 0) return
  uploading.value = true
  try {
    await store.upload(selected)
    message.success(`已上传 ${selected.length} 个文件并建立索引`)
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    uploading.value = false
    input.value = ''
  }
}

function confirmDeleteFile(fileId: string, name: string) {
  Modal.confirm({
    title: '删除文件',
    content: `确定从工作区移除「${name}」吗？知识库中的对应内容也会一并删除。`,
    okType: 'danger',
    async onOk() {
      await store.removeFile(fileId)
      message.success('已删除')
    },
  })
}

async function reindex(fileId: string) {
  try {
    await store.reindex(fileId)
    message.success('已重新索引')
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}
</script>

<template>
  <aside class="panel sidebar">
    <div class="panel-header">
      <span>工作区</span>
      <a-button type="text" size="small" @click="creating = true">
        <template #icon><PlusOutlined /></template>
      </a-button>
    </div>

    <div class="workspace-list">
      <!--
        Deliberately not `a-empty` with `:image="null"`: ant-design-vue's Empty checks
        `"type" in image`, and since `typeof null === "object"` that throws a TypeError
        during render, which aborts the whole mount and leaves a blank page.
      -->
      <div v-if="store.workspaces.length === 0" class="empty-workspaces">
        <div class="empty-title">还没有工作区</div>
        <a-button type="primary" size="small" @click="creating = true">新建工作区</a-button>
      </div>
      <a-radio-group
        v-else
        :value="store.activeWorkspaceId"
        class="workspace-radio"
        @update:value="store.selectWorkspace($event as string)"
      >
        <a-radio v-for="item in store.workspaces" :key="item.id" :value="item.id" class="workspace-item">
          <div class="workspace-meta">
            <div class="workspace-name">{{ item.name }}</div>
            <div class="muted">{{ item.file_count }} 个文件</div>
          </div>
        </a-radio>
      </a-radio-group>
    </div>

    <div class="panel-header">
      <span>文件</span>
      <label class="upload-trigger" :class="{ disabled: !store.activeWorkspaceId || uploading }">
        <input
          type="file"
          multiple
          accept=".xlsx,.xlsm,.docx"
          :disabled="!store.activeWorkspaceId || uploading"
          @change="handleUpload"
        />
        <InboxOutlined /> 上传
      </label>
    </div>

    <div class="panel-body">
      <a-spin :spinning="store.loadingFiles || uploading">
        <div v-if="files.length === 0" class="empty">
          上传 Excel 或 Word 文件<br />即可与 Agent 对话
        </div>
        <div v-for="file in files" :key="file.id" class="file-item">
          <component
            :is="file.kind === 'excel' ? FileExcelOutlined : FileWordOutlined"
            class="file-icon"
            :style="{ color: file.kind === 'excel' ? '#16a34a' : '#2563eb' }"
          />
          <div class="file-meta">
            <div class="file-name" :title="file.rel_path">{{ file.rel_path }}</div>
            <div class="file-sub">
              <a-tag :color="statusColor(file.status)" size="small">{{ statusLabel(file.status) }}</a-tag>
              <span class="muted">{{ file.chunk_count }} 段</span>
            </div>
            <div v-if="file.error" class="file-error" :title="file.error">{{ file.error }}</div>
          </div>
          <div class="file-actions">
            <!-- 提案应用后文件已在磁盘上更新，下载是用户拿到修改结果唯一途径 -->
            <a
              v-if="store.activeWorkspaceId"
              class="file-action-link"
              :href="api.downloadUrl(store.activeWorkspaceId, file.id)"
              :download="file.rel_path"
              title="下载当前文件"
            >
              <DownloadOutlined />
            </a>
            <a-button type="text" size="small" title="重新索引" @click="reindex(file.id)">
              <template #icon><ReloadOutlined /></template>
            </a-button>
            <a-button
              type="text"
              size="small"
              danger
              title="删除"
              @click="confirmDeleteFile(file.id, file.rel_path)"
            >
              <template #icon><DeleteOutlined /></template>
            </a-button>
          </div>
        </div>
      </a-spin>
    </div>

    <div v-if="store.health && !store.health.mcp_started" class="health-warning">
      文档工具服务未就绪：{{ store.health.mcp_error }}
    </div>

    <a-modal
      v-model:open="creating"
      title="新建工作区"
      ok-text="创建"
      cancel-text="取消"
      @ok="createWorkspace"
    >
      <a-input
        v-model:value="newName"
        placeholder="工作区名称，例如：2026 年度报销"
        @press-enter="createWorkspace"
      />
    </a-modal>
  </aside>
</template>

<style scoped>
.workspace-list {
  max-height: 32vh;
  overflow-y: auto;
  padding: 8px;
  border-bottom: 1px solid var(--border);
}

.workspace-radio {
  display: flex;
  flex-direction: column;
  width: 100%;
}

.empty-workspaces {
  text-align: center;
  padding: 22px 12px;
}

.empty-title {
  font-size: 13px;
  color: var(--text-muted);
  margin-bottom: 10px;
}

.workspace-item {
  display: flex;
  align-items: center;
  width: 100%;
  margin: 0;
  padding: 7px 8px;
  border-radius: 8px;
}

.workspace-item:hover {
  background: var(--surface-muted);
}

.workspace-meta {
  margin-left: 4px;
  font-size: 13px;
}

.workspace-name {
  font-weight: 500;
}

.upload-trigger {
  cursor: pointer;
  font-size: 13px;
  font-weight: 500;
  color: var(--accent);
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.upload-trigger input {
  display: none;
}

.upload-trigger.disabled {
  color: var(--text-muted);
  cursor: not-allowed;
}

.file-item {
  display: flex;
  gap: 10px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
  align-items: flex-start;
}

.file-item:hover {
  background: var(--surface-muted);
}

.file-icon {
  font-size: 17px;
  margin-top: 2px;
}

.file-meta {
  flex: 1;
  min-width: 0;
}

.file-name {
  font-size: 13px;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.file-sub {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 3px;
  font-size: 12px;
}

.file-error {
  margin-top: 3px;
  font-size: 11px;
  color: var(--warning);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.file-actions {
  display: flex;
  gap: 2px;
  align-items: center;
}

.file-action-link {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: 6px;
  color: var(--text-muted);
  font-size: 12px;
}

.file-action-link:hover {
  color: var(--accent);
  background: var(--surface-muted);
}

.health-warning {
  padding: 10px 14px;
  font-size: 12px;
  color: var(--danger);
  background: #fef2f2;
  border-top: 1px solid #fecaca;
}
</style>
