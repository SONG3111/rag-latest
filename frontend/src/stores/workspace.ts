import { defineStore } from 'pinia'
import { api } from '@/api'
import { streamChat } from '@/composables/useChatStream'
import type {
  ChatTurn,
  Citation,
  DocumentFile,
  McpTool,
  Operation,
  StoredMessage,
  Workspace,
} from '@/types'

let turnCounter = 0
const nextId = (prefix: string) => `${prefix}-${Date.now()}-${(turnCounter += 1)}`

export const useWorkspaceStore = defineStore('workspace', {
  state: () => ({
    workspaces: [] as Workspace[],
    activeWorkspaceId: null as string | null,
    files: [] as DocumentFile[],
    tools: [] as McpTool[],
    turns: [] as ChatTurn[],
    operations: [] as Operation[],
    streaming: false,
    loadingFiles: false,
    health: null as { mcp_started: boolean; tools: number; mcp_error: string | null } | null,
    lastError: null as string | null,
  }),

  getters: {
    activeWorkspace(state): Workspace | null {
      return state.workspaces.find((item) => item.id === state.activeWorkspaceId) ?? null
    },
    pendingOperations(state): Operation[] {
      return state.operations.filter((item) => item.status === 'proposed')
    },
    latestCitations(state): Citation[] {
      for (let index = state.turns.length - 1; index >= 0; index -= 1) {
        const turn = state.turns[index]
        if (turn.role === 'assistant' && turn.citations.length > 0) return turn.citations
      }
      return []
    },
  },

  actions: {
    /**
     * Look a turn up through the reactive array.
     *
     * Streaming handlers must mutate the turn via this lookup, never via a local
     * variable captured before the push. Holding the pre-push reference writes to the
     * plain object instead of the reactive proxy, so the UI does not update until
     * something else forces a re-render — which is what made replies appear only
     * after switching workspaces and back.
     */
    turnById(id: string): ChatTurn | undefined {
      return this.turns.find((turn) => turn.id === id)
    },

    async refreshHealth() {
      try {
        this.health = await api.health()
      } catch {
        // A health probe is informational: losing it must not stop the app from
        // rendering. The sidebar shows a warning from `health` being null.
        this.health = null
      }
    },

    async loadWorkspaces() {
      this.workspaces = await api.listWorkspaces()
      if (!this.activeWorkspaceId && this.workspaces.length > 0) {
        await this.selectWorkspace(this.workspaces[0].id)
      }
    },

    async createWorkspace(name: string, description?: string) {
      const created = await api.createWorkspace(name, description)
      this.workspaces = [created, ...this.workspaces]
      await this.selectWorkspace(created.id)
    },

    async deleteWorkspace(id: string) {
      await api.deleteWorkspace(id)
      this.workspaces = this.workspaces.filter((item) => item.id !== id)
      if (this.activeWorkspaceId === id) {
        this.activeWorkspaceId = null
        this.files = []
        this.turns = []
        this.operations = []
        if (this.workspaces.length > 0) await this.selectWorkspace(this.workspaces[0].id)
      }
    },

    async selectWorkspace(id: string) {
      this.activeWorkspaceId = id
      this.turns = []
      this.lastError = null
      await Promise.all([this.loadFiles(), this.loadTools(), this.loadHistory()])
    },

    async loadFiles() {
      if (!this.activeWorkspaceId) return
      this.loadingFiles = true
      try {
        this.files = await api.listFiles(this.activeWorkspaceId)
      } finally {
        this.loadingFiles = false
      }
    },

    async loadTools() {
      if (!this.activeWorkspaceId) return
      this.tools = await api.listTools(this.activeWorkspaceId)
    },

    async loadHistory() {
      if (!this.activeWorkspaceId) return
      const [messages, operations]: [StoredMessage[], Operation[]] = await Promise.all([
        api.listMessages(this.activeWorkspaceId),
        api.listOperations(this.activeWorkspaceId),
      ])
      this.turns = messages.map((message) => ({
        id: message.id,
        role: message.role === 'user' ? 'user' : 'assistant',
        content: message.content,
        citations: message.citations ?? [],
        activities: [],
        proposals: [],
        streaming: false,
      }))
      this.operations = operations
    },

    async upload(files: File[]) {
      if (!this.activeWorkspaceId) return
      await api.uploadFiles(this.activeWorkspaceId, files)
      await this.loadFiles()
    },

    async reindex(fileId: string) {
      if (!this.activeWorkspaceId) return
      await api.reindexFile(this.activeWorkspaceId, fileId)
      await this.loadFiles()
    },

    async removeFile(fileId: string) {
      if (!this.activeWorkspaceId) return
      await api.deleteFile(this.activeWorkspaceId, fileId)
      await this.loadFiles()
    },

    async send(message: string) {
      if (!this.activeWorkspaceId || this.streaming) return
      const workspaceId = this.activeWorkspaceId

      this.turns.push({
        id: nextId('user'),
        role: 'user',
        content: message,
        citations: [],
        activities: [],
        proposals: [],
        streaming: false,
      })

      const assistantId = nextId('assistant')
      this.turns.push({
        id: assistantId,
        role: 'assistant',
        content: '',
        citations: [],
        activities: [],
        proposals: [],
        streaming: true,
      })
      this.streaming = true
      this.lastError = null

      try {
        await streamChat(workspaceId, message, {
          onToken: (payload) => {
            const turn = this.turnById(assistantId)
            if (turn) turn.content += payload.text
          },
          onToolCall: (payload) => {
            const turn = this.turnById(assistantId)
            if (!turn) return
            turn.activities.push({
              id: nextId('activity'),
              tool: payload.tool,
              label: payload.label,
              status: 'running',
            })
          },
          onToolResult: () => {
            const turn = this.turnById(assistantId)
            if (!turn) return
            const running = [...turn.activities]
              .reverse()
              .find((activity) => activity.status === 'running')
            if (running) running.status = 'done'
          },
          onProposal: async (payload) => {
            const operation: Operation = {
              id: payload.operation_id,
              tool_name: payload.tool,
              rel_path: payload.path,
              summary: payload.summary,
              status: 'proposed',
              diff: payload.diff as Operation['diff'],
              result: null,
              error: null,
              backup_path: null,
              created_at: new Date().toISOString(),
              resolved_at: null,
            }
            const turn = this.turnById(assistantId)
            if (turn) turn.proposals.push(operation)
            this.operations = [operation, ...this.operations]
          },
          onCitations: (payload) => {
            const turn = this.turnById(assistantId)
            if (turn) turn.citations = payload.items as Citation[]
          },
          onDone: (payload) => {
            const turn = this.turnById(assistantId)
            if (!turn) return
            // The streamed tokens already built the message. `done` is authoritative
            // when it carries text (it is the server's final copy), but an empty one
            // must not wipe text that already arrived token by token.
            if (payload.content) turn.content = payload.content
          },
          onError: (payload) => {
            const turn = this.turnById(assistantId)
            if (turn) turn.error = payload.message
          },
        })
      } catch (error) {
        const turn = this.turnById(assistantId)
        const text = error instanceof Error ? error.message : String(error)
        if (turn) turn.error = text
        this.lastError = text
      } finally {
        const turn = this.turnById(assistantId)
        if (turn) turn.streaming = false
        this.streaming = false
        // Reload so proposals and the re-indexed file state reflect the server.
        await Promise.all([this.loadFiles(), this.loadOperations()])
      }
    },

    async loadOperations() {
      if (!this.activeWorkspaceId) return
      const operations = await api.listOperations(this.activeWorkspaceId)
      this.operations = operations
      const byId = new Map(operations.map((operation) => [operation.id, operation]))
      for (const turn of this.turns) {
        turn.proposals = turn.proposals
          .map((proposal) => byId.get(proposal.id) ?? proposal)
          .filter((proposal) => proposal.status === 'proposed')
      }
    },

    async applyOperation(operationId: string) {
      if (!this.activeWorkspaceId) return
      await api.applyOperation(this.activeWorkspaceId, operationId)
      await Promise.all([this.loadOperations(), this.loadFiles()])
    },

    async rejectOperation(operationId: string) {
      if (!this.activeWorkspaceId) return
      await api.rejectOperation(this.activeWorkspaceId, operationId)
      await this.loadOperations()
    },

    async revertOperation(operationId: string) {
      if (!this.activeWorkspaceId) return
      await api.revertOperation(this.activeWorkspaceId, operationId)
      await Promise.all([this.loadOperations(), this.loadFiles()])
    },
  },
})
