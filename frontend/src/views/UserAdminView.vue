<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { Modal, message } from 'ant-design-vue'
import { useAuthStore } from '@/stores/auth'
import { api } from '@/api'
import type { User } from '@/types'

/**
 * 账号治理。管理员的权限边界止于此：这里只列账号，看不到任何人的工作区内容
 * （服务端对 /api/workspaces/** 一律按 owner 判定，管理员不例外）。
 */
const auth = useAuthStore()
const users = ref<User[]>([])
const loading = ref(false)
const creating = ref(false)
const form = reactive({ email: '', display_name: '', password: '', role: 'member' })

async function load() {
  loading.value = true
  try {
    users.value = await api.listUsers()
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    loading.value = false
  }
}

async function create() {
  try {
    const created = await api.createUser({ ...form })
    users.value = [created, ...users.value]
    creating.value = false
    Object.assign(form, { email: '', display_name: '', password: '', role: 'member' })
    message.success(`已创建 ${created.email}`)
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}

async function toggleStatus(user: User) {
  const next = user.status === 'active' ? 'disabled' : 'active'
  try {
    const updated = await api.setUserStatus(user.id, next)
    replace(updated)
    message.success(next === 'disabled' ? '已停用' : '已启用')
  } catch (error) {
    // 最后管理员不可停用等护栏由服务端返回 409 + 中文原因，原样提示。
    message.error(error instanceof Error ? error.message : String(error))
  }
}

async function toggleRole(user: User) {
  const next = user.role === 'admin' ? 'member' : 'admin'
  try {
    replace(await api.setUserRole(user.id, next))
    message.success(next === 'admin' ? '已设为管理员' : '已取消管理员')
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}

function resetPassword(user: User) {
  let value = ''
  Modal.confirm({
    title: `重置 ${user.email} 的密码`,
    content: () =>
      h('input', {
        class: 'ant-input',
        type: 'password',
        placeholder: '新密码（至少 8 位）',
        onInput: (event: Event) => {
          value = (event.target as HTMLInputElement).value
        },
      }),
    async onOk() {
      if (value.length < 8) throw new Error('密码至少 8 位')
      replace(await api.resetUserPassword(user.id, value))
      message.success('已重置；请把新密码告知本人')
    },
  })
}

function replace(updated: User) {
  users.value = users.value.map((item) => (item.id === updated.id ? updated : item))
}

onMounted(load)
</script>

<script lang="ts">
import { h } from 'vue'
export default { name: 'UserAdminView' }
</script>

<template>
  <div class="admin-page">
    <header class="admin-header">
      <div>
        <h1 class="admin-title">账号管理</h1>
        <p class="admin-subtitle">
          当前登录：{{ auth.displayName }}（{{ auth.user?.email }}）
        </p>
      </div>
      <div class="admin-actions">
        <a-button @click="$router.push('/')">返回对话</a-button>
        <a-button type="primary" @click="creating = true">新建账号</a-button>
      </div>
    </header>

    <a-table
      :data-source="users"
      :loading="loading"
      row-key="id"
      size="middle"
      :pagination="false"
    >
      <a-table-column title="邮箱" data-index="email" />
      <a-table-column title="姓名" data-index="display_name" />
      <a-table-column title="角色" width="120">
        <template #default="{ record }">
          <a-tag :color="record.role === 'admin' ? 'blue' : 'default'">
            {{ record.role === 'admin' ? '管理员' : '成员' }}
          </a-tag>
        </template>
      </a-table-column>
      <a-table-column title="状态" width="100">
        <template #default="{ record }">
          <a-tag :color="record.status === 'active' ? 'green' : 'red'">
            {{ record.status === 'active' ? '启用' : '停用' }}
          </a-tag>
        </template>
      </a-table-column>
      <a-table-column title="最后登录" width="190">
        <template #default="{ record }">
          <span class="muted">
            {{ record.last_login_at ? new Date(record.last_login_at).toLocaleString() : '从未登录' }}
          </span>
        </template>
      </a-table-column>
      <a-table-column title="操作" width="260">
        <template #default="{ record }">
          <a-space>
            <a @click="resetPassword(record)">重置密码</a>
            <a @click="toggleRole(record)">{{ record.role === 'admin' ? '取消管理员' : '设为管理员' }}</a>
            <a @click="toggleStatus(record)">{{ record.status === 'active' ? '停用' : '启用' }}</a>
          </a-space>
        </template>
      </a-table-column>
    </a-table>

    <a-modal v-model:open="creating" title="新建账号" ok-text="创建" @ok="create">
      <a-form layout="vertical">
        <a-form-item label="邮箱" required>
          <a-input v-model:value="form.email" type="email" />
        </a-form-item>
        <a-form-item label="姓名" required>
          <a-input v-model:value="form.display_name" />
        </a-form-item>
        <a-form-item label="初始密码（至少 8 位）" required>
          <a-input v-model:value="form.password" type="password" />
        </a-form-item>
        <a-form-item label="角色">
          <a-select v-model:value="form.role">
            <a-select-option value="member">成员</a-select-option>
            <a-select-option value="admin">管理员</a-select-option>
          </a-select>
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<style scoped>
.admin-page {
  max-width: 1100px;
  margin: 0 auto;
  padding: 32px 24px;
}

.admin-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
}

.admin-title {
  margin: 0 0 4px;
  font-size: 20px;
  font-weight: 600;
}

.admin-subtitle {
  margin: 0;
  font-size: 13px;
  color: var(--text-muted, #6b7280);
}

.admin-actions {
  display: flex;
  gap: 8px;
}

.muted {
  color: var(--text-muted, #6b7280);
  font-size: 13px;
}
</style>
