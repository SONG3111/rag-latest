import { defineStore } from 'pinia'
import { api } from '@/api'
import type {
  MeetingDetail,
  MeetingHealth,
  MeetingListItem,
  MeetingSegment,
} from '@/types'

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

export const useMeetingStore = defineStore('meeting', {
  state: () => ({
    meetings: [] as MeetingListItem[],
    current: null as MeetingDetail | null,
    health: null as MeetingHealth | null,
    loading: false,
    savingNotes: false,
    error: null as string | null,
    partialTranscript: '',
  }),

  actions: {
    async bootstrap() {
      this.loading = true
      this.error = null
      try {
        const [health, meetings] = await Promise.all([
          api.meetingHealth(),
          api.listMeetings(),
        ])
        this.health = health
        this.meetings = meetings
        if (meetings.length) await this.select(meetings[0].id)
      } catch (error) {
        this.error = error instanceof Error ? error.message : String(error)
      } finally {
        this.loading = false
      }
    },

    async loadMeetings() {
      this.meetings = await api.listMeetings()
    },

    async refreshHealth() {
      try {
        this.health = await api.meetingHealth()
      } catch {
        // Health is informational and must not break the meeting view.
      }
    },

    async create(title = '', language: 'zh' | 'en' | 'auto' = 'zh') {
      const meeting = await api.createMeeting(title, language)
      await this.loadMeetings()
      this.current = meeting
      this.partialTranscript = ''
      return meeting
    },

    async select(meetingId: string) {
      this.current = await api.getMeeting(meetingId)
      this.partialTranscript = ''
    },

    async refreshCurrent() {
      if (!this.current) return
      this.current = await api.getMeeting(this.current.id)
      await this.loadMeetings()
    },

    async update(payload: {
      title?: string
      notes?: string
      language?: 'zh' | 'en' | 'auto'
    }) {
      if (!this.current) return
      this.current = await api.updateMeeting(this.current.id, payload)
      await this.loadMeetings()
    },

    async saveNotes(notes: string) {
      if (!this.current || notes === this.current.notes) return
      this.savingNotes = true
      try {
        this.current = await api.updateMeeting(this.current.id, { notes })
      } finally {
        this.savingNotes = false
      }
    },

    async remove(meetingId: string) {
      await api.deleteMeeting(meetingId)
      if (this.current?.id === meetingId) this.current = null
      await this.loadMeetings()
      if (!this.current && this.meetings.length) await this.select(this.meetings[0].id)
    },

    applyLiveSegments(segments: Array<Pick<MeetingSegment, 'id' | 'start_ms' | 'end_ms' | 'text'>>) {
      if (!this.current) return
      this.current.segments = segments.map((segment, ordinal) => ({
        ...segment,
        meeting_id: this.current!.id,
        ordinal,
      }))
    },

    async startRecordingState() {
      if (!this.current) return
      this.current.status = 'recording'
      this.current.summary_status = 'none'
      this.current.summary_markdown = ''
      this.current.segments = []
      this.partialTranscript = ''
    },

    async finishRecordingState() {
      await this.refreshCurrent()
      if (!this.current) return
      if (this.current.segments.length || this.current.notes.trim()) {
        await this.generateMinutes()
      }
    },

    async generateMinutes() {
      if (!this.current) return
      try {
        await api.generateMeetingMinutes(this.current.id)
      } catch (error) {
        const text = error instanceof Error ? error.message : String(error)
        if (!text.includes('cannot start')) throw error
      }
      this.current.summary_status = 'generating'
      const meetingId = this.current.id
      for (let attempt = 0; attempt < 150; attempt += 1) {
        await sleep(2000)
        if (this.current?.id !== meetingId) return
        const meeting = await api.getMeeting(meetingId)
        this.current = meeting
        if (meeting.summary_status !== 'generating') {
          await this.loadMeetings()
          return
        }
      }
      throw new Error('纪要生成超时，请稍后重试')
    },
  },
})
