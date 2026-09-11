/**
 * Integration check: run the real server's SSE stream through the real parser.
 *
 * The unit test proves the parser handles CRLF; this proves the actual running server
 * produces frames the parser can split, including a `proposal` frame — the event that
 * was disappearing.
 *
 * Run with:  node --experimental-strip-types sse-live-check.mjs
 */

import { drainSseEvents } from './src/composables/sse.ts'

const BASE = 'http://127.0.0.1:8000'

const listWorkspaces = async () => {
  const response = await fetch(`${BASE}/api/workspaces`)
  return response.json()
}

const response = await listWorkspaces()
if (!response.length) {
  console.log('no workspace available')
  process.exit(1)
}

const BASE_HEADERS = { 'Content-Type': 'application/json', Accept: 'text/event-stream' }

/** Consume a chat stream through the real parser and return per-event counts. */
const collect = async (workspaceId, question) => {
  const stream = await fetch(`${BASE}/api/workspaces/${workspaceId}/chat/stream`, {
    method: 'POST',
    headers: BASE_HEADERS,
    body: JSON.stringify({ message: question }),
  })

  const counts = {}
  const proposals = []
  let buffer = ''
  let firstEventAt = null
  const started = Date.now()

  for await (const chunk of stream.body) {
    buffer += new TextDecoder().decode(chunk, { stream: true })
    const { events, rest } = drainSseEvents(buffer)
    buffer = rest
    for (const event of events) {
      if (firstEventAt === null) firstEventAt = Date.now() - started
      counts[event.event] = (counts[event.event] ?? 0) + 1
      if (event.event === 'proposal') proposals.push(event.data)
    }
  }

  const { events } = drainSseEvents(buffer + '\n\n')
  for (const event of events) {
    counts[event.event] = (counts[event.event] ?? 0) + 1
    if (event.event === 'proposal') proposals.push(event.data)
  }

  return { counts, proposals, firstEventAt }
}

const failures = []

// --------------------------------------------------------------------------- //
// 1) A read question: proves tokens and tool events stream through.
// --------------------------------------------------------------------------- //
const workspace = response[0]
console.log(`workspace = ${workspace.name} (${workspace.id})`)
console.log('question  = 报销制度规定了什么？')
console.log()

const read = await collect(workspace.id, '报销制度规定了什么？')
console.log(`first event at : ${read.firstEventAt} ms`)
console.log(`event counts   : ${JSON.stringify(read.counts)}`)
console.log()

if (!read.counts.token) failures.push('no token events parsed')
if ((read.firstEventAt ?? 1e9) > 5000) {
  failures.push(`first event arrived late (${read.firstEventAt} ms) — frames are still buffering`)
}
if (!read.counts.done) failures.push('no done event parsed')

// --------------------------------------------------------------------------- //
// 2) An edit request: proves the `proposal` frame survives the parser.
// --------------------------------------------------------------------------- //
const fs = await import('node:fs/promises')

const newWorkspace = await (
  await fetch(`${BASE}/api/workspaces`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: 'SSE 提案验证' }),
  })
).json()

// Building an xlsx from Node would mean a second implementation of the format, so
// this reuses the demo workbook that `scripts/make_demo_data.py` generates.
const demoPath = new URL('../demo/2026年第一季度报销明细.xlsx', import.meta.url)
let editable = null
try {
  const bytes = await fs.readFile(demoPath)
  editable = bytes
} catch {
  console.log('demo workbook not found — skipping the proposal phase')
}

if (editable) {
  const form = new FormData()
  form.append('files', new Blob([editable]), '报销明细.xlsx')
  const uploaded = await (
    await fetch(`${BASE}/api/workspaces/${newWorkspace.id}/files`, {
      method: 'POST',
      body: form,
    })
  ).json()
  console.log(`workspace = SSE 提案验证 (${newWorkspace.id})`)
  console.log(`indexed   = ${JSON.stringify(uploaded.map((i) => [i.rel_path, i.chunk_count]))}`)
  console.log('question  = 把 BX-2026-001 的状态改成已通过')
  console.log()

  const edit = await collect(newWorkspace.id, '把 BX-2026-001 的状态改成已通过')
  console.log(`first event at : ${edit.firstEventAt} ms`)
  console.log(`event counts   : ${JSON.stringify(edit.counts)}`)
  console.log(`proposals      : ${edit.proposals.length}`)
  for (const proposal of edit.proposals) {
    console.log(`                 ${proposal.summary}`)
  }
  console.log()

  if (!edit.proposals.length) {
    failures.push('no proposal event survived parsing — the pending tab would stay empty')
  }

  await fetch(`${BASE}/api/workspaces/${newWorkspace.id}`, { method: 'DELETE' })
  console.log(`cleaned up workspace ${newWorkspace.id}`)
  console.log()
}

console.log(failures.length === 0 ? 'RESULT: PASS' : 'RESULT: FAIL')
for (const failure of failures) console.log(`  - ${failure}`)
process.exit(failures.length === 0 ? 0 : 1)
