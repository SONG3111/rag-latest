import type {
  AssistantAction,
  AssistantHealth,
  AssistantMessage,
  AssistantTool,
  CitationPreview,
  DocumentFile,
  IndexingResult,
  McpTool,
  MeetingDetail,
  MeetingHealth,
  MeetingListItem,
  NewsCategories,
  NewsDigest,
  NewsHealth,
  NewsMessage,
  NewsPreference,
  Operation,
  StoredMessage,
  Workspace,
} from './types'

const BASE = '/api'

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  if (!response.ok) {
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

  assistantHealth: () => request<AssistantHealth>('/api/assistant/health'),

  listAssistantMessages: () =>
    request<AssistantMessage[]>('/api/assistant/messages'),

  listAssistantActions: () =>
    request<AssistantAction[]>('/api/assistant/actions'),

  listAssistantTools: () =>
    request<AssistantTool[]>('/api/assistant/tools'),

  applyAssistantAction: (actionId: string) =>
    request<AssistantAction>(`/api/assistant/actions/${actionId}/apply`, {
      method: 'POST',
    }),

  rejectAssistantAction: (actionId: string) =>
    request<AssistantAction>(`/api/assistant/actions/${actionId}/reject`, {
      method: 'POST',
    }),

  meetingHealth: () => request<MeetingHealth>('/api/meetings/health'),

  listMeetings: () => request<MeetingListItem[]>('/api/meetings'),

  createMeeting: (title: string, language: 'zh' | 'en' | 'auto') =>
    request<MeetingDetail>('/api/meetings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, language }),
    }),

  getMeeting: (meetingId: string) =>
    request<MeetingDetail>(`/api/meetings/${meetingId}`),

  updateMeeting: (
    meetingId: string,
    payload: { title?: string; notes?: string; language?: 'zh' | 'en' | 'auto' },
  ) =>
    request<MeetingDetail>(`/api/meetings/${meetingId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),

  deleteMeeting: (meetingId: string) =>
    request<void>(`/api/meetings/${meetingId}`, { method: 'DELETE' }),

  generateMeetingMinutes: (meetingId: string) =>
    request<{ meeting_id: string; summary_status: string }>(
      `/api/meetings/${meetingId}/minutes`,
      { method: 'POST' },
    ),

  meetingExportUrl: (meetingId: string) =>
    `${BASE}/meetings/${meetingId}/export.md`,

  newsCategories: () => request<NewsCategories>('/api/news/categories'),

  newsPreferences: () => request<NewsPreference>('/api/news/preferences'),

  updateNewsPreferences: (categories: string[], customTopics: string[]) =>
    request<NewsPreference>('/api/news/preferences', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ categories, custom_topics: customTopics }),
    }),

  latestNewsDigest: (date?: string) =>
    request<NewsDigest>(
      `/api/news/digests/latest${date ? `?date=${encodeURIComponent(date)}` : ''}`,
    ),

  refreshNewsDigest: () =>
    request<NewsDigest>('/api/news/digests/refresh', { method: 'POST' }),

  getNewsDigest: (digestId: string) =>
    request<NewsDigest>(`/api/news/digests/${digestId}`),

  newsMessages: () => request<NewsMessage[]>('/api/news/messages'),

  newsHealth: () => request<NewsHealth>('/api/news/health'),
}
