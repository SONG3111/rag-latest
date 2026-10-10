<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const email = ref('')
const password = ref('')
const error = ref('')
const submitting = ref(false)

async function submit() {
  if (submitting.value) return
  error.value = ''
  submitting.value = true
  try {
    await auth.login(email.value.trim(), password.value)
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    await router.replace(redirect)
  } catch (caught) {
    // 后端对"账号不存在"与"密码错误"返回同一文案（不做账号枚举），这里原样展示。
    error.value = caught instanceof Error ? caught.message : String(caught)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <form class="login-card" @submit.prevent="submit">
      <h1 class="login-title">工作区文档 Agent</h1>
      <p class="login-subtitle">请使用管理员为你创建的账号登录</p>

      <label class="field">
        <span class="field-label">邮箱</span>
        <a-input v-model:value="email" type="email" autocomplete="username" required />
      </label>

      <label class="field">
        <span class="field-label">密码</span>
        <a-input-password v-model:value="password" autocomplete="current-password" required />
      </label>

      <div v-if="error" class="login-error">{{ error }}</div>

      <a-button type="primary" html-type="submit" block :loading="submitting">登录</a-button>
    </form>
  </div>
</template>

<style scoped>
.login-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: var(--page-bg, #f5f6f8);
}

.login-card {
  width: 360px;
  padding: 32px 28px;
  background: #fff;
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 14px;
  box-shadow: 0 10px 30px rgba(15, 23, 42, 0.08);
}

.login-title {
  margin: 0 0 6px;
  font-size: 19px;
  font-weight: 600;
}

.login-subtitle {
  margin: 0 0 22px;
  font-size: 13px;
  color: var(--text-muted, #6b7280);
}

.field {
  display: block;
  margin-bottom: 16px;
}

.field-label {
  display: block;
  margin-bottom: 6px;
  font-size: 13px;
  color: var(--text-muted, #6b7280);
}

.login-error {
  margin-bottom: 14px;
  padding: 8px 10px;
  font-size: 13px;
  color: #b91c1c;
  background: #fef2f2;
  border: 1px solid #fecaca;
  border-radius: 8px;
}
</style>
