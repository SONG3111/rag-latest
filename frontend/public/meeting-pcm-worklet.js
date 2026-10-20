/**
 * Downsample any AudioContext rate to 16 kHz mono s16le and post 100 ms frames.
 *
 * Meeting transcription needs raw PCM because MediaRecorder chunks (especially
 * WebM without the first header chunk) are not independently decodable. Keeping
 * resampling here also means the backend never needs FFmpeg for live audio.
 */
class MeetingPcmProcessor extends AudioWorkletProcessor {
  constructor() {
    super()
    this.targetRate = 16000
    this.sourcePosition = 0
    this.previousSample = 0
    this.output = []
    this.frameSamples = 1600
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0]
    if (!channel || channel.length === 0) return true

    const combined = new Float32Array(channel.length + 1)
    combined[0] = this.previousSample
    combined.set(channel, 1)
    const step = sampleRate / this.targetRate

    while (this.sourcePosition < channel.length) {
      const index = Math.floor(this.sourcePosition)
      const fraction = this.sourcePosition - index
      const a = combined[index]
      const b = combined[index + 1] == null ? a : combined[index + 1]
      this.output.push(a + (b - a) * fraction)
      this.sourcePosition += step
    }

    this.sourcePosition -= channel.length
    this.previousSample = channel[channel.length - 1]

    while (this.output.length >= this.frameSamples) {
      const frame = this.output.splice(0, this.frameSamples)
      const pcm = new Int16Array(frame.length)
      for (let i = 0; i < frame.length; i += 1) {
        const value = Math.max(-1, Math.min(1, frame[i]))
        pcm[i] = value < 0 ? value * 0x8000 : value * 0x7fff
      }
      this.port.postMessage(pcm.buffer, [pcm.buffer])
    }
    return true
  }
}

registerProcessor('meeting-pcm-processor', MeetingPcmProcessor)
