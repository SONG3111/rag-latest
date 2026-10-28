<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import ChatPanel from './components/ChatPanel.vue'
import InspectorPanel from './components/InspectorPanel.vue'
import WorkspaceSidebar from './components/WorkspaceSidebar.vue'
import AssistantView from './components/AssistantView.vue'
import MeetingView from './components/MeetingView.vue'
import NewsView from './components/NewsView.vue'
import { useWorkspaceStore } from './stores/workspace'

const store = useWorkspaceStore()
const mode = ref<'workspace' | 'assistant' | 'meeting' | 'news'>('workspace')

onMounted(async () => {
  await store.refreshHealth()
  if (store.health && !store.health.mcp_started) {
    message.warning('文档工具服务未启动，文件相关功能暂不可用')
  }
  try {
    await store.loadWorkspaces()
  } catch (error) {
    message.error(`加载工作区失败：${error instanceof Error ? error.message : String(error)}`)
  }
})
</script>

<template>
  <div class="app-root">
    <header class="mode-bar">
      <div class="brand">本地办公 Agent</div>
      <a-radio-group v-model:value="mode" size="small" button-style="solid">
        <a-radio-button value="workspace">文档工作区</a-radio-button>
        <a-radio-button value="assistant">邮件助手</a-radio-button>
        <a-radio-button value="meeting">会议助手</a-radio-button>
        <a-radio-button value="news">新闻雷达</a-radio-button>
      </a-radio-group>
    </header>
    <div v-if="mode === 'workspace'" class="app-shell workspace-host">
      <WorkspaceSidebar />
      <ChatPanel />
      <InspectorPanel />
    </div>
    <AssistantView v-else-if="mode === 'assistant'" />
    <MeetingView v-else-if="mode === 'meeting'" />
    <NewsView v-else />
  </div>
</template>

<style scoped>
.app-root {
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
}

.mode-bar {
  height: 48px;
  flex: 0 0 48px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 18px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  z-index: 2;
}

.brand {
  font-size: 14px;
  font-weight: 650;
  letter-spacing: 0.01em;
}

.workspace-host {
  height: calc(100vh - 48px);
}
</style>
