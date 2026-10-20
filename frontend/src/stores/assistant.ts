import { defineStore } from 'pinia'
import { api } from '@/api'
import { streamAssistantChat } from '@/composables/useChatStream'
import type {
  AssistantAction,
  AssistantHealth,
  AssistantMessage,
  AssistantTool,
  AssistantTurn,
} from '@/types'

let counter = 0
const nextId = (prefix: string) => `${prefix}-${Date.now()}-${(counter += 1)}`

export const useAssistantStore = defineStore('assistant', {
  state: () => ({
    turns: [] as AssistantTurn[],
    actions: [] as AssistantAction[],
    tools: [] as AssistantTool[],
    health: null as AssistantHealth | null,
    streaming: false,
    streamController: null as AbortController | null,
    loading: false,
    error: null as string | null,
  }),

  getters: {
    pendingActions(state): AssistantAction[] {
      return state.actions.filter((item) => item.status === 'proposed')
    },
  },

  actions: {
    async bootstrap() {
      this.loading = true
      try {
        const [health, tools] = await Promise.all([
          api.assistantHealth(),
          api.listAssistantTools(),
        ])
        this.health = health
        this.tools = tools
        await this.loadHistory()
      } catch (error) {
        this.error = error instanceof Error ? error.message : String(error)
      } finally {
        this.loading = false
      }
    },

    async loadActions() {
      this.actions = await api.listAssistantActions()
      const byId = new Map(this.actions.map((item) => [item.id, item]))
      for (const turn of this.turns) {
        turn.proposals = turn.proposals
          .map((item) => byId.get(item.id) ?? item)
      }
    },

    async loadHistory() {
      const [messages, actions] = await Promise.all([
        api.listAssistantMessages(),
        api.listAssistantActions(),
      ])
      this.actions = actions
      const byId = new Map(actions.map((item) => [item.id, item]))
      this.turns = messages.map((message: AssistantMessage) => {
        const ids = message.meta?.action_ids ?? []
        return {
          id: message.id,
          role: message.role === 'user' ? 'user' : 'assistant',
          content: message.content,
          activities: [],
          proposals: ids
            .map((id) => byId.get(id))
            .filter((item): item is AssistantAction => Boolean(item)),
          streaming: false,
          messageId: message.id,
        }
      })
    },

    async send(message: string) {
      if (this.streaming) return
      this.turns.push({
        id: nextId('user'),
        role: 'user',
        content: message,
        activities: [],
        proposals: [],
        streaming: false,
      })
      const assistantId = nextId('assistant')
      this.turns.push({
        id: assistantId,
        role: 'assistant',
        content: '',
        activities: [],
        proposals: [],
        streaming: true,
      })
      this.streaming = true
      this.error = null
      const controller = new AbortController()
      this.streamController = controller
      const turn = () => this.turns.find((item) => item.id === assistantId)
      try {
        await streamAssistantChat(
          message,
          {
            onToken: (payload) => {
              const item = turn()
              if (item) item.content += payload.text
            },
            onToolCall: (payload) => {
              const item = turn()
              if (!item) return
              item.activities.push({
                id: nextId('activity'),
                tool: payload.tool,
                label: payload.label,
                status: 'running',
              })
            },
            onToolResult: () => {
              const item = turn()
              if (!item) return
              const running = [...item.activities]
                .reverse()
                .find((activity) => activity.status === 'running')
              if (running) running.status = 'done'
            },
            onProposal: (payload) => {
              const item = turn()
              if (!item) return
              const action = payload as unknown as AssistantAction
              item.proposals.push(action)
              this.actions = [action, ...this.actions]
            },
            onDone: (payload) => {
              const item = turn()
              if (!item) return
              if (payload.content) item.content = payload.content
              if (payload.message_id) item.messageId = payload.message_id
            },
            onError: (payload) => {
              const item = turn()
              if (item) item.error = payload.message
            },
          },
          controller.signal,
        )
      } catch (error) {
        const item = turn()
        const aborted = error instanceof DOMException && error.name === 'AbortError'
        if (aborted) {
          if (item?.content) item.content += '\n\n（已停止生成，以上为已生成的部分。）'
        } else {
          const text = error instanceof Error ? error.message : String(error)
          if (item) item.error = text
          this.error = text
        }
      } finally {
        const item = turn()
        if (item) item.streaming = false
        this.streaming = false
        this.streamController = null
        await Promise.all([this.loadActions(), this.refreshHealth()])
      }
    },

    stop() {
      this.streamController?.abort()
      this.streamController = null
    },

    async applyAction(actionId: string) {
      try {
        await api.applyAssistantAction(actionId)
      } finally {
        await this.loadActions()
      }
    },

    async rejectAction(actionId: string) {
      await api.rejectAssistantAction(actionId)
      await this.loadActions()
    },

    async refreshHealth() {
      try {
        this.health = await api.assistantHealth()
      } catch {
        // Health is informational; a failed probe must not break the page.
      }
    },
  },
})
