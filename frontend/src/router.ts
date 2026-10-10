import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import ChatShell from '@/views/ChatShell.vue'
import LoginView from '@/views/LoginView.vue'
import UserAdminView from '@/views/UserAdminView.vue'

/**
 * 路由是登录态的前端入口：`/login` 之外的一切都要求已认证，`/admin/users` 还要求
 * 管理员。真正的强制在服务端（401/403/404），这里只负责别把界面渲染成"看起来能用"
 * 的样子——前端守卫是体验，不是安全边界。
 */
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: LoginView },
    { path: '/', name: 'chat', component: ChatShell },
    {
      path: '/admin/users',
      name: 'users',
      component: UserAdminView,
      meta: { adminOnly: true },
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  auth.registerUnauthorizedHandler()
  await auth.ensureLoaded()

  if (to.name !== 'login' && !auth.user) {
    return { name: 'login', query: to.fullPath === '/' ? {} : { redirect: to.fullPath } }
  }
  if (to.name === 'login' && auth.user) {
    return { name: 'chat' }
  }
  if (to.meta.adminOnly && !auth.isAdmin) {
    return { name: 'chat' }
  }
  return true
})
