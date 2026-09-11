/**
 * Server-Sent Events framing.
 *
 * Extracted from the chat client so it can be tested directly: the framing rules are
 * exactly where a subtle mistake is invisible in a browser until events go missing.
 *
 * The rule that matters here: a frame ends at a blank line, and the blank line is
 * `\r\n\r\n` when the server uses CRLF (which `sse-starlette` does). Splitting the
 * buffer on `'\n\n'` therefore never matches, because `\r\n\r\n` contains no `\n\n`
 * substring — every frame piles up until the connection closes, and the trailing
 * flush then mis-parses the whole batch as one event.
 */

export interface SseEvent {
  event: string
  data: unknown
}

export function drainSseEvents(buffer: string): { events: SseEvent[]; rest: string } {
  const events: SseEvent[] = []
  let event = 'message'
  const dataLines: string[] = []
  let cursor = 0

  const emit = () => {
    if (dataLines.length === 0) {
      event = 'message'
      return
    }
    const raw = dataLines.join('\n')
    let data: unknown
    try {
      data = JSON.parse(raw)
    } catch {
      data = { raw }
    }
    events.push({ event, data })
    event = 'message'
    dataLines.length = 0
  }

  for (;;) {
    const newline = buffer.indexOf('\n', cursor)
    if (newline === -1) break

    // Strip a trailing CR so CRLF and LF behave identically.
    let line = buffer.slice(cursor, newline)
    if (line.endsWith('\r')) line = line.slice(0, -1)
    cursor = newline + 1

    if (line === '') {
      emit()
    } else if (line.startsWith('event:')) {
      event = line.slice(6).trim()
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).trimStart())
    }
  }

  return { events, rest: buffer.slice(cursor) }
}
