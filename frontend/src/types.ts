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
