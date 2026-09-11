/**
 * Demonstrates the reactivity bug behind "the reply only shows up after switching
 * workspaces and back", and proves the fix.
 *
 * Run with:  node reactivity-check.cjs
 *
 * This mirrors the chat store: a reactive array of turn objects plus a render effect
 * that depends on the last turn's content. `effect` is the primitive Vue's own
 * `watchEffect` is built on.
 */

const { reactive, effect } = require('@vue/reactivity')

const state = reactive({ turns: [] })

let renders = 0
let seenContent = null

effect(() => {
  renders += 1
  const last = state.turns[state.turns.length - 1]
  seenContent = last ? last.content : null
})

// --------------------------------------------------------------------------- //
// The buggy shape: hold the object in a local variable and mutate that.
// Pushing it does not hand the local variable the reactive proxy, so writes land on
// the raw object and the UI does not update until something else triggers a render.
// --------------------------------------------------------------------------- //
const prePush = { id: 'a1', content: '', streaming: true }
state.turns.push(prePush)
const rendersAfterPush = renders
prePush.content = '已修改 C3'

const buggySeen = seenContent
console.log(
  `pre-push reference -> render effect sees ${JSON.stringify(buggySeen)}` +
    `   (renders ${rendersAfterPush} -> ${renders})`,
)

// --------------------------------------------------------------------------- //
// The fixed shape: look the turn up through the reactive array on every update.
// --------------------------------------------------------------------------- //
state.turns.length = 0
const id = 'a2'
state.turns.push({ id, content: '', streaming: true })
const rendersBeforeFix = renders

const byId = () => state.turns.find((turn) => turn.id === id)
byId().content = '已修改 C3'

const fixedSeen = seenContent
console.log(
  `lookup by id       -> render effect sees ${JSON.stringify(fixedSeen)}` +
    `   (renders ${rendersBeforeFix} -> ${renders})`,
)

console.log()
const bugReproduced = buggySeen === ''
const fixWorks = fixedSeen === '已修改 C3'

console.log(`reproduced the bug : ${bugReproduced}`)
console.log(`lookup-by-id works : ${fixWorks}`)
console.log()
console.log('RESULT:', bugReproduced && fixWorks ? 'PASS' : 'FAIL')

process.exit(bugReproduced && fixWorks ? 0 : 1)
