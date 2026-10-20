import { onBeforeUnmount, ref } from 'vue'
import type { MeetingSegment } from '@/types'

interface MeetingRecorderHandlers {
  onSegments: (segments: Array<Pick<MeetingSegment, 'id' | 'start_ms' | 'end_ms' | 'text'>>) => void
  onPartial: (text: string) => void
  onRecorded: () => void
  onFailed: (message: string) => void
}

interface TranscriptEvent {
  type: 'transcript'
  segments: Array<Pick<MeetingSegment, 'id' | 'start_ms' | 'end_ms' | 'text'>>
  partial?: string
}

interface PartialEvent {
  type: 'partial'
  text: string
}

interface StateEvent {
  type: 'state'
  state: 'loading_model' | 'recording' | 'recorded' | 'interrupted' | 'failed'
  error?: string | null
}

interface MetricsEvent {
  type: 'metrics'
  compute_lag_seconds: number
}

interface ErrorEvent {
  type: 'error'
  code?: string
  message: string
}

type ServerEvent =
  | TranscriptEvent
  | PartialEvent
  | StateEvent
  | MetricsEvent
  | ErrorEvent

export function useMeetingRecorder(handlers: MeetingRecorderHandlers) {
  const recording = ref(false)
  const starting = ref(false)
  const stopping = ref(false)
  const loadingModel = ref(false)
  const elapsedSeconds = ref(0)
  const partialText = ref('')
  const computeLagSeconds = ref(0)
  const error = ref<string | null>(null)
  const sourceLabel = ref('')

  let socket: WebSocket | null = null
  let audioContext: AudioContext | null = null
  let worklet: AudioWorkletNode | null = null
  let silentGain: GainNode | null = null
  let streams: MediaStream[] = []
  let timer: number | null = null
  let finalizeTimer: number | null = null
  let stopRequested = false
  let finalized = false

  const supported =
    typeof navigator !== 'undefined' &&
    Boolean(navigator.mediaDevices?.getUserMedia) &&
    Boolean(navigator.mediaDevices?.getDisplayMedia)

  function setError(message: string) {
    error.value = message
    handlers.onFailed(message)
  }

  function resetCapture() {
    loadingModel.value = false
    computeLagSeconds.value = 0
    if (timer !== null) window.clearInterval(timer)
    timer = null
    if (finalizeTimer !== null) window.clearTimeout(finalizeTimer)
    finalizeTimer = null
    worklet?.disconnect()
    silentGain?.disconnect()
    for (const stream of streams) {
      for (const track of stream.getTracks()) track.stop()
    }
    streams = []
    worklet = null
    silentGain = null
    if (audioContext) {
      void audioContext.close()
      audioContext = null
    }
  }

  function closeSocket() {
    if (socket && socket.readyState === WebSocket.OPEN) socket.close()
    socket = null
  }

  function finalizeOnce(failed = false) {
    if (finalized) return
    finalized = true
    recording.value = false
    stopping.value = false
    loadingModel.value = false
    resetCapture()
    closeSocket()
    if (failed) handlers.onFailed(error.value || '录音或转写失败')
    else handlers.onRecorded()
  }

  async function start(meetingId: string) {
    if (recording.value || starting.value) return
    if (!supported) {
      setError('当前浏览器不支持麦克风与系统音频采集，请使用最新版 Chrome 或 Edge。')
      return
    }
    starting.value = true
    error.value = null
    partialText.value = ''
    elapsedSeconds.value = 0
    loadingModel.value = false
    computeLagSeconds.value = 0
    stopRequested = false
    finalized = false

    try {
      const microphone = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      })
      streams.push(microphone)

      let display: MediaStream | null = null
      try {
        display = await navigator.mediaDevices.getDisplayMedia({
          video: true,
          audio: true,
        })
      } catch {
        resetCapture()
        setError('未获得屏幕或标签页共享权限。请重新开始，并选择共享系统音频。')
        return
      }
      streams.push(display)

      const hasSystemAudio = display.getAudioTracks().length > 0
      if (!hasSystemAudio) {
        const continueWithMic = window.confirm(
          '没有捕获到系统声音，线上会议的其他参会人可能听不到。是否仅使用麦克风继续？',
        )
        if (!continueWithMic) {
          resetCapture()
          setError('已取消录音：请重新开始并勾选“分享系统音频”。')
          return
        }
        for (const track of display.getTracks()) track.stop()
        display = null
      }
      sourceLabel.value = hasSystemAudio ? '麦克风 + 系统声音' : '仅麦克风'

      audioContext = new AudioContext({ latencyHint: 'interactive' })
      if (audioContext.state === 'suspended') await audioContext.resume()
      await audioContext.audioWorklet.addModule('/meeting-pcm-worklet.js')
      worklet = new AudioWorkletNode(audioContext, 'meeting-pcm-processor')
      silentGain = audioContext.createGain()
      silentGain.gain.value = 0
      worklet.connect(silentGain)
      silentGain.connect(audioContext.destination)

      const microphoneSource = audioContext.createMediaStreamSource(microphone)
      const microphoneGain = audioContext.createGain()
      microphoneGain.gain.value = hasSystemAudio ? 0.55 : 1
      microphoneSource.connect(microphoneGain)
      microphoneGain.connect(worklet)

      if (display) {
        const displaySource = audioContext.createMediaStreamSource(display)
        const displayGain = audioContext.createGain()
        displayGain.gain.value = 0.55
        displaySource.connect(displayGain)
        displayGain.connect(worklet)
      }

      const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
      socket = new WebSocket(
        `${scheme}://${window.location.host}/api/meetings/${meetingId}/audio`,
      )
      socket.binaryType = 'arraybuffer'
      let readySettled = false
      let resolveReady: () => void = () => {}
      let rejectReady: (error: Error) => void = () => {}
      const ready = new Promise<void>((resolve, reject) => {
        resolveReady = () => {
          if (readySettled) return
          readySettled = true
          resolve()
        }
        rejectReady = (cause: Error) => {
          if (readySettled) return
          readySettled = true
          reject(cause)
        }
      })
      void ready.catch(() => undefined)

      socket.onmessage = (message) => {
        let event: ServerEvent
        try {
          event = JSON.parse(String(message.data)) as ServerEvent
        } catch {
          return
        }
        if (event.type === 'state' && event.state === 'recording') {
          loadingModel.value = false
          resolveReady()
          return
        }
        if (event.type === 'transcript') {
          handlers.onSegments(event.segments)
          partialText.value = event.partial || ''
        } else if (event.type === 'partial') {
          partialText.value = event.text
        } else if (event.type === 'error') {
          error.value = event.message
          rejectReady(new Error(event.message))
        } else if (event.type === 'state') {
          if (event.state === 'loading_model') {
            loadingModel.value = true
          } else if (event.state === 'recorded' || event.state === 'interrupted') {
            finalizeOnce(false)
          } else if (event.state === 'failed') {
            error.value = event.error || '转写失败'
            finalizeOnce(true)
          }
        } else if (event.type === 'metrics') {
          computeLagSeconds.value = Math.max(0, event.compute_lag_seconds || 0)
        }
      }

      await new Promise<void>((resolve, reject) => {
        if (!socket) {
          reject(new Error('无法创建录音连接'))
          return
        }
        socket.onclose = () => {
          rejectReady(new Error(error.value || '转写服务在准备阶段断开'))
        }
        socket.onopen = () => resolve()
        socket.onerror = () => {
          rejectReady(new Error('无法连接会议转写服务'))
          reject(new Error('无法连接会议转写服务'))
        }
      })
      await ready
      socket.onclose = () => {
        if (!finalized && stopRequested) {
          finalizeOnce(Boolean(error.value))
        } else if (!finalized) {
          error.value = error.value || '转写连接意外断开，已保存断开前的转写。'
          finalizeOnce(true)
        }
      }

      worklet.port.onmessage = (event: MessageEvent<ArrayBuffer>) => {
        if (socket?.readyState === WebSocket.OPEN) socket.send(event.data)
      }

      recording.value = true
      timer = window.setInterval(() => {
        elapsedSeconds.value += 1
      }, 1000)
    } catch (cause) {
      resetCapture()
      closeSocket()
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      starting.value = false
    }
  }

  async function stop() {
    if (!recording.value || stopping.value) return
    stopping.value = true
    stopRequested = true
    partialText.value = ''
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(new Uint8Array(0))
    } else {
      finalizeOnce(false)
      return
    }
    finalizeTimer = window.setTimeout(() => {
      if (!finalized) {
        error.value = '等待本地转写结束时超时，已保存已确认的转写。'
        finalizeOnce(true)
      }
    }, 35000)
  }

  onBeforeUnmount(() => {
    stopRequested = true
    if (socket?.readyState === WebSocket.OPEN) socket.send(new Uint8Array(0))
    resetCapture()
    closeSocket()
  })

  return {
    supported,
    recording,
    starting,
    stopping,
    loadingModel,
    elapsedSeconds,
    partialText,
    computeLagSeconds,
    error,
    sourceLabel,
    start,
    stop,
  }
}
