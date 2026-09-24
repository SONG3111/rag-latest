/**
 * SSE client for the chat endpoint.
 *
 * `EventSource` is not used because it cannot issue a POST with a JSON body, and the
 * conversation message does not belong in a query string. Instead the response body
 * is read as a stream and the SSE framing is parsed by hand — which is also what lets
 * the UI react to each event type as it arrives rather than after the turn completes.
 */

import { drainSseEvents } from './sse'

export interface StreamHandlers {
  onToken?: (payload: { text: string }) => void
  /** Reasoning-channel fragments; rendered in a collapsible "thinking" section. */
  onThinking?: (payload: { text: string }) => void
  /** Status notices from the backend (model fallback, turn timeout). */
  onNotice?: (payload: { message: string }) => void
  onToolCall?: (payload: { tool: string; args: Record<string, unknown>; label: string }) => void
  onToolResult?: (payload: { tool_call_id: string; content: string }) => void
  onProposal?: (payload: {
    operation_id: string
    tool: string
    summary: string
    path: string
    diff: unknown[]
  }) => void
  onCitations?: (payload: { items: unknown[] }) => void
  /** Post-turn suggested questions; rendered as clickable chips. */
  onFollowups?: (payload: { items: string[] }) => void
  onDone?: (payload: { content: string; run_id?: string; message_id?: string }) => void
  onError?: (payload: { message: string }) => void
}

export async function streamChat(
  workspaceId: string,
  message: string,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`/api/workspaces/${workspaceId}/chat/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({ message }),
    signal,
  })

  // 非 2xx 时后端会给出可读的 detail（如韧性快速失败的 503 文案），不要丢弃它。
  if (!response.ok) {
    const detail = await response
      .json()
      .then((body: { detail?: string }) => body?.detail)
      .catch(() => undefined)
    throw new Error(detail ?? `对话请求失败：${response.status} ${response.statusText}`)
  }
  if (!response.body) {
    throw new Error('对话请求失败：响应体为空')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  // Frames can be split across network chunks, so only complete lines are consumed
  // and the remainder is carried into the next read.
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    const { events, rest } = drainSseEvents(buffer)
    buffer = rest
    for (const parsed of events) {
      dispatch(parsed.event, parsed.data as never, handlers)
    }
  }

  // A truncated final frame still carries its data line even without the trailing
  // blank line, so drain whatever is left.
  buffer += decoder.decode()
  const { events } = drainSseEvents(buffer + '\n\n')
  for (const parsed of events) {
    dispatch(parsed.event, parsed.data as never, handlers)
  }
}

function dispatch(event: string, data: any, handlers: StreamHandlers): void {
  switch (event) {
    case 'token':
      handlers.onToken?.(data)
      break
    case 'thinking':
      handlers.onThinking?.(data)
      break
    case 'notice':
      handlers.onNotice?.(data)
      break
    case 'tool_call':
      handlers.onToolCall?.(data)
      break
    case 'tool_result':
      handlers.onToolResult?.(data)
      break
    case 'proposal':
      handlers.onProposal?.(data)
      break
    case 'citations':
      handlers.onCitations?.(data)
      break
    case 'followups':
      handlers.onFollowups?.(data)
      break
    case 'done':
      handlers.onDone?.(data)
      break
    case 'error':
      handlers.onError?.(data)
      break
    default:
      break
  }
}
