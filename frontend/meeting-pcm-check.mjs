import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'

let ProcessorClass = null
const context = {
  sampleRate: 48000,
  AudioWorkletProcessor: class {
    constructor() {
      this.port = { postMessage: () => {} }
    }
  },
  registerProcessor: (name, processor) => {
    assert.equal(name, 'meeting-pcm-processor')
    ProcessorClass = processor
  },
  Float32Array,
  Int16Array,
  Math,
}
vm.createContext(context)
vm.runInContext(
  fs.readFileSync(new URL('./public/meeting-pcm-worklet.js', import.meta.url), 'utf8'),
  context,
)
assert.ok(ProcessorClass, 'worklet must register its processor')

const processor = new ProcessorClass()
const frames = []
processor.port.postMessage = (buffer) => frames.push(buffer)

// One second of a 440 Hz sine wave at the browser's 48 kHz rate should produce
// ten 100 ms frames (1,600 samples each at 16 kHz).
const blockSize = 128
const source = new Float32Array(48000)
for (let i = 0; i < source.length; i += 1) {
  source[i] = Math.sin((2 * Math.PI * 440 * i) / 48000) * 0.5
}
for (let offset = 0; offset < source.length; offset += blockSize) {
  processor.process([[source.slice(offset, offset + blockSize)]])
}

assert.equal(frames.length, 10)
for (const frame of frames) {
  assert.equal(frame.byteLength, 3200)
}
const first = new Int16Array(frames[0])
assert.ok(first.some((value) => value !== 0), 'PCM frame must contain signal')
assert.ok(first.every((value) => value >= -32768 && value <= 32767))

console.log('RESULT: PASS — 48 kHz float audio downsamples into 16 kHz s16le frames')
