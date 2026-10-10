import type {
  CitationPreview,
  DocumentFile,
  IndexingResult,
  McpTool,
  Operation,
  StoredMessage,
  User,
  Workspace,
} from './types'

const BASE = '/api'

/**
 * Cookie 名与后端 CookieCsrfTokenRepository 的默认值一致。
 *
 * 注意必须从 Cookie 里取值：后端为防 BREACH 会把"渲染用"的令牌做异或掩码
 * （/api/auth/csrf 返回的就是掩码后的值），而校验时拿它与 Cookie 里的原始值比对，
 * 掩码值必然对不上。这也是 Spring Security 官方 SPA 配方的写法。
 */
const CSRF_COOKIE = 'XSRF-TOKEN'
const CSRF_HEADER = 'X-XSRF-TOKEN'

export function csrfToken(): string | null {
  const match = document.cookie.match(new RegExp(`(?:^|;\\s*)${CSRF_COOKIE}=([^;]+)`))
  return match ? decodeURIComponent(match[1]) : null
}

/** 会话失效时的回调（由 auth store 注册，用于跳登录页）。 */
let onUnauthorized: (() => void) | null = null
export function setUnauthorizedHandler(handler: () => void) {
  onUnauthorized = handler
}

/** SSE 聊天走的是独立的 fetch，不走 request()，因此单独暴露这个通知入口。 */
export function notifyUnauthorized() {
  onUnauthorized?.()
}

/** 变更类请求要带的 CSRF 头；缺失时先取一次令牌。 */
export async function csrfHeaders(): Promise<Record<string, string>> {
  if (!csrfToken()) await primeCsrf()
  const token = csrfToken()
  return token ? { [CSRF_HEADER]: token } : {}
}

/**
 * 取一次 CSRF 令牌。令牌是延迟创建的：不主动请求，浏览器就永远拿不到
 * XSRF-TOKEN Cookie，登录（本身就是变更类请求）也会因缺令牌被拒。
 */
export async function primeCsrf(): Promise<void> {
  await fetch(`${BASE}/auth/csrf`, { credentials: 'same-origin' })
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? 'GET').toUpperCase()
  const headers = new Headers(init?.headers)
  if (method !== 'GET' && method !== 'HEAD') {
    // 令牌可能因为后端重启/过期而缺失或失效：缺失时先取一次再发。
    if (!csrfToken()) await primeCsrf()
    const token = csrfToken()
    if (token) headers.set(CSRF_HEADER, token)
  }

  // credentials 必带：登录态是 HttpOnly 会话 Cookie，不带就等于匿名。
  const response = await fetch(url, { ...init, headers, credentials: 'same-origin' })
  if (!response.ok) {
    if (response.status === 401) {
      onUnauthorized?.()
    }
    let detail = `${response.status} ${response.statusText}`
    try {
      const body = await response.json()
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      /* response had no JSON body */
    }
    throw new Error(detail)
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const api = {
  // ------------------------------------------------------------------ //
  // 认证与账号
  // ------------------------------------------------------------------ //
  login: (email: string, password: string) =>
    request<User>(`${BASE}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    }),

  logout: () => request<void>(`${BASE}/auth/logout`, { method: 'POST' }),

  me: () => request<User>(`${BASE}/auth/me`),

  listUsers: () => request<User[]>(`${BASE}/admin/users`),

  createUser: (payload: { email: string; display_name: string; password: string; role: string }) =>
    request<User>(`${BASE}/admin/users`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),

  resetUserPassword: (userId: string, password: string) =>
    request<User>(`${BASE}/admin/users/${userId}/password`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    }),

  setUserStatus: (userId: string, status: 'active' | 'disabled') =>
    request<User>(`${BASE}/admin/users/${userId}/status`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    }),

  setUserRole: (userId: string, role: 'admin' | 'member') =>
    request<User>(`${BASE}/admin/users/${userId}/role`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role }),
    }),

  listWorkspaces: () => request<Workspace[]>(`${BASE}/workspaces`),

  createWorkspace: (name: string, description?: string) =>
    request<Workspace>(`${BASE}/workspaces`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, description }),
    }),

  deleteWorkspace: (id: string) =>
    request<void>(`${BASE}/workspaces/${id}`, { method: 'DELETE' }),

  listFiles: (workspaceId: string) =>
    request<DocumentFile[]>(`${BASE}/workspaces/${workspaceId}/files`),

  uploadFiles: (workspaceId: string, files: File[]) => {
    const form = new FormData()
    files.forEach((file) => form.append('files', file))
    return request<IndexingResult[]>(`${BASE}/workspaces/${workspaceId}/files`, {
      method: 'POST',
      body: form,
    })
  },

  reindexFile: (workspaceId: string, fileId: string) =>
    request<IndexingResult>(`${BASE}/workspaces/${workspaceId}/files/${fileId}/reindex`, {
      method: 'POST',
    }),

  deleteFile: (workspaceId: string, fileId: string) =>
    request<void>(`${BASE}/workspaces/${workspaceId}/files/${fileId}`, { method: 'DELETE' }),

  downloadUrl: (workspaceId: string, fileId: string) =>
    `${BASE}/workspaces/${workspaceId}/files/${fileId}/download`,

  listTools: (workspaceId: string) =>
    request<McpTool[]>(`${BASE}/workspaces/${workspaceId}/tools`),

  listMessages: (workspaceId: string) =>
    request<StoredMessage[]>(`${BASE}/workspaces/${workspaceId}/messages`),

  setMessageFeedback: (workspaceId: string, messageId: string, feedback: 'up' | 'down' | 'none') =>
    request<StoredMessage>(
      `${BASE}/workspaces/${workspaceId}/messages/${messageId}/feedback`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ feedback }),
      },
    ),

  previewCitation: (workspaceId: string, file: string, location: string) =>
    request<CitationPreview>(
      `${BASE}/workspaces/${workspaceId}/preview?file=${encodeURIComponent(file)}&location=${encodeURIComponent(location)}`,
    ),

  listOperations: (workspaceId: string) =>
    request<Operation[]>(`${BASE}/workspaces/${workspaceId}/operations`),

  applyOperation: (workspaceId: string, operationId: string) =>
    request<Operation>(`${BASE}/workspaces/${workspaceId}/operations/${operationId}/apply`, {
      method: 'POST',
    }),

  rejectOperation: (workspaceId: string, operationId: string) =>
    request<Operation>(`${BASE}/workspaces/${workspaceId}/operations/${operationId}/reject`, {
      method: 'POST',
    }),

  revertOperation: (workspaceId: string, operationId: string) =>
    request<Operation>(`${BASE}/workspaces/${workspaceId}/operations/${operationId}/revert`, {
      method: 'POST',
    }),

  health: () => request<{ status: string; mcp_started: boolean; tools: number; mcp_error: string | null }>('/health'),
}
