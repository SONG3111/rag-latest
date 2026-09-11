/**
 * Regression test for SSE frame parsing.
 *
 * The bug this guards against: the server terminates frames with CRLF, so splitting
 * the buffer on '\n\n' never matches. Every event then sat in the buffer until the
 * connection closed, and the trailing flush mis-parsed the whole batch as a single
 * event — which is why tool/proposal events went missing and text only appeared at
 * the very end.
 *
 * Run with:  node sse-framing-check.mjs
 */

import { drainSseEvents } from './src/composables/sse.ts'

let failures = 0

function check(label, condition, detail = '') {
  if (condition) {
    console.log(`  PASS  ${label}`)
  } else {
    console.log(`  FAIL  ${label}${detail ? ` — ${detail}` : ''}`)
    failures += 1
  }
}

console.log('CRLF frames (what sse-starlette actually sends)')
{
  const raw =
    'event: token\r\ndata: {"text":"报销"}\r\n\r\n' +
    'event: tool_call\r\ndata: {"tool":"read_range"}\r\n\r\n' +
    'event: proposal\r\ndata: {"operation_id":"op1"}\r\n\r\n'
  const { events, rest } = drainSseEvents(raw)
  check('three events parsed', events.length === 3, `got ${events.length}`)
  check('event names preserved', events.map((e) => e.event).join(',') === 'token,tool_call,proposal')
  check('payloads decoded', events[2]?.data?.operation_id === 'op1')
  check('no leftover buffer', rest === '')
}

console.log('\nLF frames (other servers)')
{
  const raw = 'event: token\ndata: {"text":"甲"}\n\n' + 'event: done\ndata: {"text":"乙"}\n\n'
  const { events } = drainSseEvents(raw)
  check('two events parsed', events.length === 2, `got ${events.length}`)
  check('payloads decoded', events[1]?.data?.text === '乙')
}

console.log('\npartial frame is buffered, not emitted')
{
  const raw = 'event: token\r\ndata: {"text":"半'
  const { events, rest } = drainSseEvents(raw)
  check('nothing emitted yet', events.length === 0)
  check('remainder retained', rest.includes('半'))

  const continuation = drainSseEvents(rest + '句"}\r\n\r\n')
  check('completes once the frame ends', continuation.events.length === 1)
  check('payload intact', continuation.events[0].data.text === '半句')
}

console.log('\nframe split across network chunks')
{
  const first = 'event: token\r\nda'
  const second = 'ta: {"text":"跨包"}\r\n\r\n'
  const step1 = drainSseEvents(first)
  check('first chunk yields nothing', step1.events.length === 0)
  const step2 = drainSseEvents(step1.rest + second)
  check('second chunk completes the frame', step2.events.length === 1)
  check('payload intact', step2.events[0].data.text === '跨包')
}

console.log('\ntrailing frame without a blank line is still drained')
{
  const { events } = drainSseEvents('event: done\r\ndata: {"text":"收尾"}\r\n\r\n')
  check('done event parsed', events.length === 1 && events[0].event === 'done')
}

console.log('\nwhy the previous implementation failed (documentation)')
{
  // The old parser split on '\n\n'. Show that this cannot match CRLF frames at all.
  const crlfFrame = 'event: proposal\r\ndata: {"operation_id":"op1"}\r\n\r\n'
  check(
    'CRLF frame contains no "\\n\\n"',
    crlfFrame.indexOf('\n\n') === -1,
    'if this ever matches, the framing assumption changed',
  )
  check(
    'old split yields zero frames',
    crlfFrame.split('\n\n').filter((part) => part.trim() !== '').length === 1,
    'one unsplit blob reaches the fallback parser instead of a clean frame',
  )
}

console.log()
console.log(failures === 0 ? 'RESULT: PASS' : `RESULT: FAIL (${failures})`)
process.exit(failures === 0 ? 0 : 1)
