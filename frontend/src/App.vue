<script setup lang="ts">
import { onMounted } from 'vue'
import { message } from 'ant-design-vue'
import ChatPanel from './components/ChatPanel.vue'
import InspectorPanel from './components/InspectorPanel.vue'
import WorkspaceSidebar from './components/WorkspaceSidebar.vue'
import { useWorkspaceStore } from './stores/workspace'

const store = useWorkspaceStore()

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
  <div class="app-shell">
    <WorkspaceSidebar />
    <ChatPanel />
    <InspectorPanel />
  </div>
</template>
