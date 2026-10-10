<script setup lang="ts">
// 应用外壳只剩路由出口：登录页、对话壳与账号管理页由 router 决定渲染谁。
// 鉴权与数据加载都下沉到各自视图（登录页不碰工作区数据，管理员页不碰对话状态）。
import { useAuthStore } from './stores/auth'

// 注册"会话失效"回调：任何一个接口返回 401，都由 auth store 统一清状态并跳登录页。
useAuthStore().registerUnauthorizedHandler()
</script>

<template>
  <router-view />
</template>
