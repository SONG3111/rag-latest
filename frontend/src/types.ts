export interface Workspace {
  id: string
  name: string
  description: string | null
  created_at: string
  updated_at: string
  file_count: number
}

export interface DocumentFile {
  id: string
  rel_path: string
  kind: 'excel' | 'word'
  size_bytes: number
  status: 'pending' | 'indexing' | 'indexed' | 'failed'
  chunk_count: number
  error: string | null
  indexed_at: string | null
  created_at: string
}

export interface Citation {
  file: string | null
  location: string | null
  snippet: string | null
  score: number | null
  score_source?: string | null
  parent_location?: string | null
}

export interface OperationDiffEntry {
  cell?: string
  before?: unknown
  after?: unknown
  location?: string
  paragraph_index?: number
  row?: number
  column?: number
  occurrences?: number
}

export interface Operation {
  id: string
  tool_name: string
  rel_path: string
  summary: string
  status: 'proposed' | 'applied' | 'rejected' | 'failed'
  diff: OperationDiffEntry[]
  result: Record<string, unknown> | null
  error: string | null
  backup_path: string | null
  created_at: string
  resolved_at: string | null
}

export interface StoredMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  citations: Citation[] | null
  tool_calls: unknown[] | null
  feedback: 'up' | 'down' | null
  created_at: string
}

export interface McpTool {
  name: string
  description: string
  read_only: boolean
  destructive: boolean
  requires_approval: boolean
  schema: Record<string, unknown>
}

export interface AssistantTool {
  name: string
  description: string
  read_only: boolean
  destructive: boolean
  requires_approval: boolean
  schema: Record<string, unknown>
}

export interface AssistantAction {
  id: string
  conversation_id: string
  source_message_id: string | null
  tool_name: string
  arguments: Record<string, unknown>
  preview: Record<string, unknown>
  summary: string
  status: 'proposed' | 'approved' | 'executing' | 'applied' | 'rejected' | 'failed' | 'expired'
  approved_by: string | null
  approval_channel: string | null
  result: Record<string, unknown> | null
  error: string | null
  created_at: string
  expires_at: string
  resolved_at: string | null
}

export interface AssistantMessage {
  id: string
  conversation_id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  external_event_id: string | null
  meta: {
    action_ids?: string[]
    [key: string]: unknown
  }
  created_at: string
}

export interface AssistantHealth {
  enabled: boolean
  mail_mcp_started: boolean
  mail_auth_ready: boolean
  mail_error: string | null
  feishu_configured: boolean
  feishu_connected: boolean
  feishu_error: string | null
  pending_actions: number
}

export interface AssistantActivity {
  id: string
  tool: string
  label: string
  status: 'running' | 'done'
}

export interface AssistantTurn {
  id: string
  role: 'user' | 'assistant'
  content: string
  activities: AssistantActivity[]
  proposals: AssistantAction[]
  streaming: boolean
  messageId?: string
  error?: string
}

export type MeetingStatus = 'draft' | 'recording' | 'recorded' | 'interrupted' | 'failed'
export type MeetingSummaryStatus = 'none' | 'generating' | 'ready' | 'failed'

export interface MeetingSegment {
  id: string
  meeting_id: string
  ordinal: number
  start_ms: number
  end_ms: number
  text: string
}

export interface MeetingListItem {
  id: string
  title: string
  status: MeetingStatus
  summary_status: MeetingSummaryStatus
  language: 'zh' | 'en' | 'auto'
  started_at: string | null
  ended_at: string | null
  duration_seconds: number
  error: string | null
  created_at: string
  updated_at: string
}

export interface MeetingDetail extends MeetingListItem {
  notes: string
  summary_markdown: string
  segments: MeetingSegment[]
}

export interface MeetingHealth {
  enabled: boolean
  available: boolean
  ready: boolean
  loading: boolean
  backend: string
  model: string
  language: string
  device: string
  compute_type: string
  error: string | null
}

export interface MeetingChatTurn {
  id: string
  role: 'user' | 'assistant'
  content: string
  activities: AssistantActivity[]
  streaming: boolean
  error?: string
}

export interface IndexingResult {
  file_id: string
  rel_path: string
  chunk_count: number
  vector_count: number
  status: string
  error: string | null
}

/** A tool invocation rendered inline in the conversation. */
export interface ToolActivity {
  id: string
  tool: string
  label: string
  status: 'running' | 'done' | 'failed'
  detail?: string
}

/** One turn in the conversation, assembled client-side from the SSE stream. */
export interface ChatTurn {
  id: string
  role: 'user' | 'assistant'
  content: string
  /** Reasoning-channel text streamed during this turn; never persisted server-side. */
  thinking?: string
  /** Backend status notice for this turn (model fallback, timeout, ...). */
  notice?: string
  citations: Citation[]
  activities: ToolActivity[]
  proposals: Operation[]
  streaming: boolean
  error?: string
  /** Server-side message id, present once the turn has been persisted. */
  messageId?: string
  /** Thumbs feedback attached to the persisted message. */
  feedback?: 'up' | 'down' | null
}

/** Original text window behind one citation, fetched by the preview endpoint. */
export interface CitationPreview {
  kind: 'excel' | 'word_paragraphs' | 'word_table'
  file: string
  location: string
  /** excel */
  sheet?: string
  start_row?: number
  rows?: unknown[][]
  /** word paragraphs */
  paragraphs?: { index: number; text: string; is_heading?: boolean }[]
  /** word table */
  table_index?: number
  /**
   * What to highlight: for excel/word_table the 0-based row index within
   * `rows`; for word_paragraphs the paragraph's absolute index.
   */
  highlight?: number | null
}

export interface NewsCategory {
  id: string
  label: string
  description: string
}

export interface NewsCategories {
  categories: NewsCategory[]
  defaults: string[]
  custom_topic_limit: number
}

export interface NewsPreference {
  categories: string[]
  custom_topics: string[]
}

export interface NewsSourceRef {
  article_id: string
  source_name: string
  source_domain: string
  title: string
  original_title: string
  url: string
  published_at: string
  time_basis: 'published' | 'observed'
  language: string
}

export interface NewsEvent {
  id: string
  category: string
  category_label: string
  title: string
  original_title: string
  summary: string
  why_it_matters: string
  importance: number
  confidence: number
  source_count: number
  published_at: string
  time_basis: 'published' | 'observed'
  sources: NewsSourceRef[]
}

export interface NewsDigest {
  id: string
  edition_date: string
  window_start: string
  window_end: string
  categories: string[]
  custom_topics: string[]
  trigger: 'scheduled' | 'manual'
  status: 'pending' | 'running' | 'ready' | 'failed'
  degraded: boolean
  warnings: string[]
  source_stats: {
    attempted?: number
    succeeded?: number
    failed?: number
    articles?: number
  }
  error: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
  events: NewsEvent[]
}

export interface NewsHealth {
  enabled: boolean
  timezone: string
  generation_mode: 'on_demand'
  cache_minutes: number
  providers: Record<
    string,
    {
      enabled: boolean
      base_url?: string
      last_run?: Record<string, number>
    }
  >
  last_digest_status: string | null
  last_digest_at: string | null
  last_error: string | null
}

export interface NewsMessage {
  id: string
  conversation_id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  external_event_id: string | null
  meta: Record<string, unknown>
  created_at: string
}

export interface NewsTurn {
  id: string
  role: 'user' | 'assistant'
  content: string
  activities: AssistantActivity[]
  streaming: boolean
  messageId?: string
  error?: string
}
