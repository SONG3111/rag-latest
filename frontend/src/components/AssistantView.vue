<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { ReloadOutlined } from '@ant-design/icons-vue'
import { useAssistantStore } from '@/stores/assistant'
import AssistantChatPanel from './AssistantChatPanel.vue'
import AssistantActionCard from './AssistantActionCard.vue'

const store = useAssistantStore()
const health = computed(() => store.health)

function statusType(value: boolean | undefined): string {
  return value ? 'success' : 'default'
}

async function refresh() {
  await Promise.all([store.refreshHealth(), store.loadHistory()])
}

onMounted(() => {
  void store.bootstrap()
})
</script>

<template>
  <div class="assistant-shell">
    <aside class="panel assistant-side">
      <div class="panel-header">
        <span>连接与审批</span>
        <a-button type="text" size="small" title="刷新" @click="refresh">
          <template #icon><ReloadOutlined /></template>
        </a-button>
      </div>
      <div class="panel-body side-body">
        <div class="section-title">服务状态</div>
        <div class="status-row">
          <span>飞书邮箱工具</span>
          <a-tag :color="statusType(health?.mail_mcp_started)" size="small">
            {{ health?.mail_mcp_started ? '已连接' : '未连接' }}
          </a-tag>
        </div>
        <div class="status-row">
          <span>飞书用户授权</span>
          <a-tag :color="statusType(health?.mail_auth_ready)" size="small">
            {{ health?.mail_auth_ready ? '已就绪' : '需授权' }}
          </a-tag>
        </div>
        <div class="status-row">
          <span>飞书长连接</span>
          <a-tag :color="statusType(health?.feishu_connected)" size="small">
            {{ health?.feishu_configured ? (health?.feishu_connected ? '在线' : '离线') : '未配置' }}
          </a-tag>
        </div>
        <div v-if="health?.mail_error" class="warning">{{ health.mail_error }}</div>
        <div v-if="health?.feishu_error" class="warning">{{ health.feishu_error }}</div>
        <div v-if="health && !health.mail_auth_ready" class="hint">
          先运行 <code>python -m mcp_feishu_server.auth</code> 完成飞书用户授权。
        </div>

        <div class="section-title pending-title">
          待审批 <span class="count">{{ store.pendingActions.length }}</span>
        </div>
        <div v-if="store.pendingActions.length === 0" class="empty-mini">
          没有等待确认的操作
        </div>
        <AssistantActionCard
          v-for="action in store.pendingActions"
          :key="action.id"
          :action="action"
          compact
        />

        <div class="section-title tools-title">工具</div>
        <div v-for="tool in store.tools" :key="tool.name" class="tool-row">
          <code>{{ tool.name }}</code>
          <a-tag :color="tool.requires_approval ? 'orange' : 'green'" size="small">
            {{ tool.requires_approval ? '需审批' : '只读' }}
          </a-tag>
        </div>
      </div>
    </aside>
    <AssistantChatPanel />
  </div>
</template>

<style scoped>
.assistant-shell {
  display: grid;
  grid-template-columns: 330px minmax(0, 1fr);
  height: calc(100vh - 48px);
  overflow: hidden;
  background: var(--surface-muted);
}

.assistant-side {
  border-right: 1px solid var(--border);
}

.side-body {
  padding: 14px;
}

.section-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-muted);
  letter-spacing: 0.02em;
  margin: 4px 0 9px;
}

.pending-title,
.tools-title {
  margin-top: 22px;
}

.count {
  color: var(--warning);
}

.status-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
  padding: 5px 0;
}

.warning {
  margin-top: 8px;
  color: var(--danger);
  font-size: 12px;
  line-height: 1.6;
  word-break: break-word;
}

.hint {
  margin-top: 9px;
  padding: 9px 10px;
  border-radius: 8px;
  background: #fffdf7;
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.7;
}

.hint code {
  font-size: 11px;
}

.empty-mini {
  padding: 10px;
  text-align: center;
  color: var(--text-muted);
  font-size: 12px;
}

.tool-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 5px 0;
  font-size: 12px;
}

.tool-row code {
  overflow: hidden;
  text-overflow: ellipsis;
}

@media (max-width: 980px) {
  .assistant-shell {
    grid-template-columns: 280px minmax(0, 1fr);
  }
}
</style>
