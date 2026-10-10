import { defineStore } from 'pinia'
import { api, primeCsrf, setUnauthorizedHandler } from '@/api'
import type { User } from '@/types'

/**
 * 登录态。
 *
 * 会话本身由 HttpOnly Cookie 承载（前端读不到，也不需要读）；这里存的是"当前是谁"
 * 这份视图，用来做路由守卫、管理员入口的显隐与登出。冷启动时用 GET /api/auth/me
 * 探一次——401 就是未登录，不需要另设"是否登录过"的本地标记（那样会和服务端状态打架）。
 */
export const useAuthStore = defineStore('auth', {
  state: () => ({
    user: null as User | null,
    ready: false,
    loading: false,
  }),

  getters: {
    isAdmin: (state) => state.user?.role === 'admin',
    displayName: (state) => state.user?.display_name ?? '',
  },

  actions: {
    /**
     * 冷启动恢复：解析一次会话。无论成功失败都置 ready，避免路由守卫死等。
     */
    async ensureLoaded() {
      if (this.ready || this.loading) return
      this.loading = true
      try {
        this.user = await api.me()
        // 会话有效时顺手把 CSRF 令牌准备好，登录后第一次写操作就不会因缺令牌失败。
        await primeCsrf()
      } catch {
        this.user = null
      } finally {
        this.loading = false
        this.ready = true
      }
    },

    async login(email: string, password: string) {
      // 登录本身是变更类请求：先拿令牌再发，否则会被 CSRF 拦成 403。
      await primeCsrf()
      this.user = await api.login(email, password)
      this.ready = true
    },

    async logout() {
      try {
        await api.logout()
      } finally {
        this.user = null
      }
    },

    /** 会话在服务端失效（过期/被停用/被登出）时由 api 层回调。 */
    handleUnauthorized() {
      this.user = null
      this.ready = true
    },

    registerUnauthorizedHandler() {
      setUnauthorizedHandler(() => {
        const wasLoggedIn = this.user !== null
        this.handleUnauthorized()
        // 只在"原本以为自己登录着"时跳转：登录接口自身的 401（密码错误）
        // 不该把用户甩到登录页（他本来就在登录页）。
        if (wasLoggedIn && window.location.pathname !== '/login') {
          window.location.assign('/login')
        }
      })
    },
  },
})
